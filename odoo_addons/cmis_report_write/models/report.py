# -*- coding: utf-8 -*-
# Copyright 2016 ACSONE SA/NV
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from collections import namedtuple
from base64 import b64decode
import os
import time
import mimetypes
import logging
from cStringIO import StringIO
from openerp import api, models, _
from openerp.exceptions import UserError
from openerp.tools.safe_eval import safe_eval


logger = logging.getLogger(__name__)


class Report(models.Model):

    _inherit = 'report'

    @api.v7
    def get_pdf(self, cr, uid, ids, report_name, html=None, data=None,
                context=None):
        """This method generates and returns pdf version of a report.
        """
        # put the report_name into the context to use it in method
        #  _save_in_attachment
        ctx = dict(context or {}, report_name=report_name)
        return super(Report, self).get_pdf(
            cr, uid, ids, report_name, html, data, context=ctx)

    def _save_in_attachment(self, cr, uid, attachment_vals, context=None):
        # Get the ir.actions.report.xml record we are working on.
        report_name = context.get('report_name')
        report_xml = self._get_report_from_name(cr, uid, report_name)
        res_id = attachment_vals['res_id']
        res_model = attachment_vals['res_model']
        record = self.pool[res_model].browse(cr, uid, res_id)
        vals = None
        if report_xml.attachment:
            file_name = self._attachment_filename(
                cr, uid, [record], report_xml).get(record.id)
            if file_name:
                vals = super(Report, self)._save_in_attachment(
                    cr, uid, attachment_vals, context=context)
        if report_xml.cmis_filename:
            # pylint: disable=unexpected-keyword-arg
            self._save_in_cmis(
                cr, uid, attachment_vals, report_xml, context=context)
        return vals

    @api.v7
    def _check_attachment_use(self, cr, uid, ids, report_xml):
        records = self.pool[report_xml.model].browse(cr, uid, ids)
        cmis_filenames = self._get_cmis_filename(cr, uid, records, report_xml)
        save_in_attachment = super(Report, self)._check_attachment_use(
            cr, uid, ids, report_xml)
        for res_id in ids:
            cmis_filename = cmis_filenames.get(res_id)
            if res_id not in save_in_attachment and cmis_filename:
                # insert the cmis_filename into the list even if we don't want
                # to store the report_xml into an attachment but only into
                # cmis. This is required to force the call to the
                # _savie_in_attachment method
                save_in_attachment[res_id] = cmis_filename
        return save_in_attachment

    @api.model
    def _get_cmis_filename(self, records, report_xml):
        return dict(
            (record.id,
             self._safe_eval(report_xml.cmis_filename, report_xml, record)
             ) for record in records if report_xml.cmis_filename)

    @api.model
    def _get_eval_context(self, report_xml, record):
        return {'object': record,
                'time': time,
                '_': _,
                'user': self.env.user,
                'context': self.env.context}

    @api.model
    def _safe_eval(self, source, report_xml, record):
        return safe_eval(
            source,
            self._get_eval_context(report_xml, record)
        )

    @api.model
    def _save_in_cmis(self, attachment_vals, report_xml):
        res_id = attachment_vals['res_id']
        res_model = attachment_vals['res_model']
        record = self.env[res_model].browse(res_id)
        report_xml = self.env['ir.actions.report.xml'].browse(report_xml.id)
        cmis_filename = self._get_cmis_filename(
            [record], report_xml).get(record.id)
        if not cmis_filename:
            # can be false to allow condition in filename
            return
        cmis_parent_folder = self._get_cmis_parent_folder(
            report_xml, record, cmis_filename)
        cmis_filename = os.path.basename(cmis_filename)
        doc_info = self._create_or_update_cmis_document(
            attachment_vals, report_xml, record, cmis_filename,
            cmis_parent_folder)
        someDoc = doc_info.doc
        cmis_objectId = someDoc.getObjectId()
        return cmis_objectId

    @api.model
    def _get_cmis_parent_folder(self, report_xml, record, cmis_filename):
        if report_xml.cmis_folder_field_id:
            # the report_xml must be saved into the folder referenced by the
            # CmisFolder field
            field_name = report_xml.cmis_folder_field_id.name
            field = record._fields[field_name]
            cmis_backend = field.get_backend(self.env)
            root_objectId = getattr(record, field_name)
            if not root_objectId:
                # the folder must be initialized on demand
                field.create_value(record)
                root_objectId = getattr(record, field_name)
        else:
            root_objectId = report_xml.cmis_backend_id.initial_directory_write
            cmis_backend = report_xml.cmis_backend_id
        # the generated name can contains sub directories
        path = os.path.dirname(cmis_filename) or '/'
        return cmis_backend.get_folder_by_path(
            path, create_if_not_found=True,
            cmis_parent_objectid=root_objectId)

    def get_mimetype(self, file_name):
        return mimetypes.guess_type(file_name)[0]

    @api.model
    def _create_or_update_cmis_document(self, attachment_vals, report_xml,
                                        record, file_name, cmis_parent_folder):
        """Create or update a cmis document according to
        cmis_duplicate_handler.
        return the created or update cmis doc
        """
        cmis_qry = ("SELECT cmis:objectId FROM cmis:document WHERE "
                    "IN_FOLDER('%s') AND cmis:name='%s'" %
                    (cmis_parent_folder.getObjectId(), file_name))
        logger.debug("Query CMIS with %s", cmis_qry)
        rs = cmis_parent_folder.repository.query(cmis_qry)
        is_new = False
        num_found_items = rs.getNumItems()
        if (num_found_items == 0 or
                report_xml.cmis_duplicate_handler == 'increment'):
            if num_found_items > 0:
                name, ext = os.path.splitext(file_name)
                testname = name + '(*)' + ext
                rs = cmis_parent_folder.getChildren(
                    filter='cmis:name=%s' % testname)
                file_name = name + '(%d)' % rs.getNumItems() + ext
            doc = self._create_cmis_document(
                attachment_vals, report_xml, record, file_name,
                cmis_parent_folder)
            return UniqueDocInfo(doc, is_new)
        if (num_found_items > 0 and
                report_xml.cmis_duplicate_handler == 'new_version'):
            doc = cmis_parent_folder.repository.getObject(
                rs.getResults()[0].getObjectId())
            doc = self._update_cmis_document(
                attachment_vals, report_xml, record, file_name,
                doc)
            return UniqueDocInfo(doc, is_new)

        raise UserError(
            _('Document "%s" already exists in CMIS') % (file_name))

    @api.model
    def _create_cmis_document(self, attachment_vals, report_xml, record,
                              file_name, cmis_parent_folder):
        props = {
            'cmis:name': file_name,
        }
        if report_xml.cmis_objectTypeId:
            props['cmis:objectTypeId'] = report_xml.cmis_objectTypeId
        props.update(self._get_cmis_properties(report_xml, record))
        mimetype = self.get_mimetype(file_name)
        doc = cmis_parent_folder.createDocument(
            file_name,
            properties=props,
            contentFile=StringIO(b64decode(attachment_vals['datas'])),
            contentType=mimetype
        )
        return doc

    @api.model
    def _update_cmis_document(self, attachment_vals, report_xml, record,
                              file_name, cmis_doc):
        # increment version
        props = self._get_cmis_properties(report_xml, record)
        if 'cmis:secondaryObjectTypeIds' in props:
            # no update aspects
            del props['cmis:secondaryObjectTypeIds']
        mimetype = self.get_mimetype(file_name)
        cmis_doc = cmis_doc.checkout()
        cmis_doc = cmis_doc.checkin(
            checkinComment=_("Generated by Odoo"),
            contentFile=StringIO(b64decode(attachment_vals['datas'])),
            contentType=mimetype,
            major=False,
            properties=props
        )

        return cmis_doc

    @api.model
    def _get_cmis_properties(self, report_xml, record):
        if report_xml.cmis_properties:
            return self._safe_eval(
                report_xml.cmis_properties, report_xml, record
            )
        return {}

UniqueDocInfo = namedtuple('UniqueDoc', ['doc', 'is_new'])