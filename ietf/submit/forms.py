# Copyright The IETF Trust 2011-2022, All Rights Reserved
# -*- coding: utf-8 -*-


import io
import os
import re
import datetime
import email
import sys
import tempfile
import xml2rfc
from contextlib import ExitStack

from email.utils import formataddr
from unidecode import unidecode
from urllib.parse import urljoin

from django import forms
from django.conf import settings
from django.utils.html import mark_safe, format_html # type:ignore
from django.urls import reverse as urlreverse
from django.utils import timezone
from django.utils.encoding import force_str

import debug                            # pyflakes:ignore

from ietf.doc.models import Document
from ietf.group.models import Group
from ietf.ietfauth.utils import has_role
from ietf.doc.fields import SearchableDocAliasesField
from ietf.doc.models import DocAlias
from ietf.ipr.mail import utc_from_string
from ietf.meeting.models import Meeting
from ietf.message.models import Message
from ietf.name.models import FormalLanguageName, GroupTypeName
from ietf.submit.models import Submission, Preapproval
from ietf.submit.utils import validate_submission_name, validate_submission_rev, validate_submission_document_date, remote_ip
from ietf.submit.parsers.pdf_parser import PDFParser
from ietf.submit.parsers.plain_parser import PlainParser
from ietf.submit.parsers.xml_parser import XMLParser
from ietf.utils import log
from ietf.utils.draft import PlaintextDraft
from ietf.utils.text import normalize_text
from ietf.utils.timezone import date_today
from ietf.utils.xmldraft import XMLDraft, XMLParseError


class SubmissionBaseUploadForm(forms.Form):
    xml = forms.FileField(label='.xml format', required=True)

    def __init__(self, request, *args, **kwargs):
        super(SubmissionBaseUploadForm, self).__init__(*args, **kwargs)

        self.remote_ip = remote_ip(request)

        self.request = request
        self.in_first_cut_off = False
        self.cutoff_warning = ""
        self.shutdown = False
        self.set_cutoff_warnings()

        self.group = None
        self.filename = None
        self.revision = None
        self.title = None
        self.abstract = None
        self.authors = []
        self.parsed_draft = None
        self.file_types = []
        self.file_info = {}             # indexed by file field name, e.g., 'txt', 'xml', ...
        self.xml_version = None
        # No code currently (14 Sep 2017) uses this class directly; it is
        # only used through its subclasses.  The two assignments below are
        # set to trigger an exception if it is used directly only to make
        # sure that adequate consideration is made if it is decided to use it
        # directly in the future.  Feel free to set these appropriately to
        # avoid the exceptions in that case:
        self.formats = None             # None will raise an exception in clean() if this isn't changed in a subclass
        self.base_formats = None        # None will raise an exception in clean() if this isn't changed in a subclass

    def set_cutoff_warnings(self):
        now = timezone.now()
        meeting = Meeting.get_current_meeting()
        if not meeting:
            return
        #
        cutoff_00 = meeting.get_00_cutoff()
        cutoff_01 = meeting.get_01_cutoff()
        reopen    = meeting.get_reopen_time()
        #
        cutoff_00_str = cutoff_00.strftime("%Y-%m-%d %H:%M %Z")
        cutoff_01_str = cutoff_01.strftime("%Y-%m-%d %H:%M %Z")
        reopen_str    = reopen.strftime("%Y-%m-%d %H:%M %Z")

        # Workaround for IETF107. This would be better handled by a refactor that allowed meetings to have no cutoff period.
        if cutoff_01 >= reopen:
            return

        if cutoff_00 == cutoff_01:
            if now.date() >= (cutoff_00.date() - meeting.idsubmit_cutoff_warning_days) and now.date() < cutoff_00.date():
                self.cutoff_warning = ( 'The last submission time for Internet-Drafts before %s is %s.<br><br>' % (meeting, cutoff_00_str))
            elif now <= cutoff_00:
                self.cutoff_warning = (
                    'The last submission time for new Internet-Drafts before the meeting is %s.<br>'
                    'After that, you will not be able to submit Internet-Drafts until after %s (IETF-meeting local time)' % (cutoff_00_str, reopen_str, ))
        else:
            if now.date() >= (cutoff_00.date() - meeting.idsubmit_cutoff_warning_days) and now.date() < cutoff_00.date():
                self.cutoff_warning = ( 'The last submission time for new documents (i.e., version -00 Internet-Drafts) before %s is %s.<br><br>' % (meeting, cutoff_00_str) +
                                        'The last submission time for revisions to existing documents before %s is %s.<br>' % (meeting, cutoff_01_str) )
            elif now.date() >= cutoff_00.date() and now <= cutoff_01:
                # We are in the first_cut_off
                if now < cutoff_00:
                    self.cutoff_warning = (
                        'The last submission time for new documents (i.e., version -00 Internet-Drafts) before the meeting is %s.<br>'
                        'After that, you will not be able to submit a new document until after %s (IETF-meeting local time)' % (cutoff_00_str, reopen_str, ))
                else:  # No 00 version allowed
                    self.cutoff_warning = (
                        'The last submission time for new documents (i.e., version -00 Internet-Drafts) was %s.<br>'
                        'You will not be able to submit a new document until after %s (IETF-meeting local time).<br><br>'
                        'You can still submit a version -01 or higher Internet-Draft until %s' % (cutoff_00_str, reopen_str, cutoff_01_str, ))
                    self.in_first_cut_off = True
        if now > cutoff_01 and now < reopen:
            self.cutoff_warning = (
                'The last submission time for the I-D submission was %s.<br><br>'
                'The I-D submission tool will be reopened after %s (IETF-meeting local time).' % (cutoff_01_str, reopen_str))
            self.shutdown = True

    def clean_file(self, field_name, parser_class):
        f = self.cleaned_data[field_name]
        if not f:
            return f

        self.file_info[field_name] = parser_class(f).critical_parse()
        if self.file_info[field_name].errors:
            raise forms.ValidationError(self.file_info[field_name].errors)
        return f

    def clean_xml(self):
        return self.clean_file("xml", XMLParser)

    def clean(self):
        def format_messages(where, e, log_msgs):
            m = str(e)
            if m:
                m = [m]
            else:
                import traceback
                typ, val, tb = sys.exc_info()
                m = traceback.format_exception(typ, val, tb)
                m = [ l.replace('\n ', ':\n ') for l in m ]
            msgs = [s for s in (["Error from xml2rfc (%s):" % (where,)] + m + log_msgs) if s]
            return msgs

        if self.shutdown and not has_role(self.request.user, "Secretariat"):
            raise forms.ValidationError('The submission tool is currently shut down')

        # check general submission rate thresholds before doing any more work
        today = date_today()
        self.check_submissions_thresholds(
            "for the same submitter",
            dict(remote_ip=self.remote_ip, submission_date=today),
            settings.IDSUBMIT_MAX_DAILY_SAME_SUBMITTER, settings.IDSUBMIT_MAX_DAILY_SAME_SUBMITTER_SIZE,
        )
        self.check_submissions_thresholds(
            "across all submitters",
            dict(submission_date=today),
            settings.IDSUBMIT_MAX_DAILY_SUBMISSIONS, settings.IDSUBMIT_MAX_DAILY_SUBMISSIONS_SIZE,
        )

        for ext in self.formats:
            f = self.cleaned_data.get(ext, None)
            if not f:
                continue
            self.file_types.append('.%s' % ext)
        if not ('.txt' in self.file_types or '.xml' in self.file_types):
            if not self.errors:
                raise forms.ValidationError('Unexpected submission file types; found %s, but %s is required' % (', '.join(self.file_types), ' or '.join(self.base_formats)))

        # Determine the draft name and revision. Try XML first.
        if self.cleaned_data.get('xml'):
            xml_file = self.cleaned_data.get('xml')
            tfn = None
            with ExitStack() as stack:
                @stack.callback
                def cleanup():  # called when context exited, even in case of exception
                    if tfn is not None:
                        os.unlink(tfn)

                # We need to write the xml file to disk in order to hand it
                # over to the xml parser.  XXX FIXME: investigate updating
                # xml2rfc to be able to work with file handles to in-memory
                # files.
                name, ext = os.path.splitext(os.path.basename(xml_file.name))
                with tempfile.NamedTemporaryFile(prefix=name+'-',
                                                 suffix='.xml',
                                                 mode='wb+',
                                                 delete=False) as tf:
                    tfn = tf.name
                    for chunk in xml_file.chunks():
                        tf.write(chunk)

                try:
                    xml_draft = XMLDraft(tfn)
                except XMLParseError as e:
                    msgs = format_messages('xml', e, e.parser_msgs())
                    self.add_error('xml', msgs)
                    return
                except Exception as e:
                    self.add_error('xml', f'Error parsing XML Internet-Draft: {e}')
                    return

                self.filename = xml_draft.filename
                self.revision = xml_draft.revision
        elif self.cleaned_data.get('txt'):
            # no XML available, extract from the text if we have it
            # n.b., this code path is unused until a subclass with a 'txt' field is created.
            txt_file = self.cleaned_data['txt']
            txt_file.seek(0)
            bytes = txt_file.read()
            try:
                text = bytes.decode(self.file_info['txt'].charset)
                self.parsed_draft = PlaintextDraft(text, txt_file.name)
                self.filename = self.parsed_draft.filename
                self.revision = self.parsed_draft.revision
            except (UnicodeDecodeError, LookupError) as e:
                self.add_error('txt', 'Failed decoding the uploaded file: "%s"' % str(e))

        rev_error = validate_submission_rev(self.filename, self.revision)
        if rev_error:
            raise forms.ValidationError(rev_error)

        # The following errors are likely noise if we have previous field
        # errors:
        if self.errors:
            raise forms.ValidationError('')

        if not self.filename:
            raise forms.ValidationError("Could not extract a valid Internet-Draft name from the upload.  "
                "To fix this in a text upload, please make sure that the full Internet-Draft name including "
                "revision number appears centered on its own line below the document title on the "
                "first page.  In an xml upload, please make sure that the top-level <rfc/> "
                "element has a docName attribute which provides the full Internet-Draft name including "
                "revision number.")

        if not self.revision:
            raise forms.ValidationError("Could not extract a valid Internet-Draft revision from the upload.  "
                "To fix this in a text upload, please make sure that the full Internet-Draft name including "
                "revision number appears centered on its own line below the document title on the "
                "first page.  In an xml upload, please make sure that the top-level <rfc/> "
                "element has a docName attribute which provides the full Internet-Draft name including "
                "revision number.")

        self.check_for_old_uppercase_collisions(self.filename)    

        if self.cleaned_data.get('txt') or self.cleaned_data.get('xml'):
            # check group
            self.group = self.deduce_group(self.filename)
            # check existing
            existing = Submission.objects.filter(name=self.filename, rev=self.revision).exclude(state__in=("posted", "cancel", "waiting-for-draft"))
            if existing:
                raise forms.ValidationError(
                    format_html(
                        'A submission with same name and revision is currently being processed. <a href="{}">Check the status here.</a>',
                        urljoin(
                            settings.IDTRACKER_BASE_URL,
                            urlreverse("ietf.submit.views.submission_status", kwargs={'submission_id': existing[0].pk}),
                        )
                    )
                )

            # cut-off
            if self.revision == '00' and self.in_first_cut_off:
                raise forms.ValidationError(mark_safe(self.cutoff_warning))
            # check thresholds that depend on the draft / group
            self.check_submissions_thresholds(
                "for the Internet-Draft %s" % self.filename,
                dict(name=self.filename, rev=self.revision, submission_date=today),
                settings.IDSUBMIT_MAX_DAILY_SAME_DRAFT_NAME, settings.IDSUBMIT_MAX_DAILY_SAME_DRAFT_NAME_SIZE,
            )
            if self.group:
                self.check_submissions_thresholds(
                    "for the group \"%s\"" % (self.group.acronym),
                    dict(group=self.group, submission_date=today),
                    settings.IDSUBMIT_MAX_DAILY_SAME_GROUP, settings.IDSUBMIT_MAX_DAILY_SAME_GROUP_SIZE,
                )
        return super().clean()

    @staticmethod
    def check_for_old_uppercase_collisions(name):
        possible_collision = Document.objects.filter(name__iexact=name).first()
        if possible_collision and possible_collision.name != name:
            raise forms.ValidationError(
                f"Case-conflicting draft name found: {possible_collision.name}. "
                "Please choose a different draft name. Case-conflicting names with "
                "the small number of legacy Internet-Drafts with names containing "
                "upper-case letters are not permitted."
            )

    @staticmethod
    def check_submissions_thresholds(which, filter_kwargs, max_amount, max_size):
        submissions = Submission.objects.filter(**filter_kwargs)

        if len(submissions) > max_amount:
            raise forms.ValidationError("Max submissions %s has been reached for today (maximum is %s submissions)." % (which, max_amount))
        if sum(s.file_size for s in submissions if s.file_size) > max_size * 1024 * 1024:
            raise forms.ValidationError("Max uploaded amount %s has been reached for today (maximum is %s MB)." % (which, max_size))

    @staticmethod
    def deduce_group(name):
        """Figure out group from name or previously submitted draft, returns None if individual."""
        existing_draft = Document.objects.filter(name=name, type="draft")
        if existing_draft:
            group = existing_draft[0].group
            if group and group.type_id not in ("individ", "area"):
                return group
            else:
                return None
        else:
            name_parts = name.split("-")
            if len(name_parts) < 3:
                raise forms.ValidationError("The Internet-Draft name \"%s\" is missing a third part, please rename it" % name)

            if name.startswith('draft-ietf-') or name.startswith("draft-irtf-"):
                if name_parts[1] == "ietf":
                    group_type = "wg"
                elif name_parts[1] == "irtf":
                    group_type = "rg"
                else:
                    group_type = None

                # first check groups with dashes
                for g in Group.objects.filter(acronym__contains="-", type=group_type):
                    if name.startswith('draft-%s-%s-' % (name_parts[1], g.acronym)):
                        return g

                try:
                    return Group.objects.get(acronym=name_parts[2], type=group_type)
                except Group.DoesNotExist:
                    raise forms.ValidationError('There is no active group with acronym \'%s\', please rename your Internet-Draft' % name_parts[2])

            elif name.startswith("draft-rfc-"):
                return Group.objects.get(acronym="iesg")
            elif name.startswith("draft-rfc-editor-") or name.startswith("draft-rfced-") or name.startswith("draft-rfceditor-"):
                return Group.objects.get(acronym="rfceditor")
            else:
                ntype = name_parts[1].lower()
                # This covers group types iesg, iana, iab, ise, and others:
                if GroupTypeName.objects.filter(slug=ntype).exists():
                    group = Group.objects.filter(acronym=ntype).first()
                    if group:
                        return group
                    else:
                        raise forms.ValidationError('Internet-Draft names starting with draft-%s- are restricted, please pick a different name' % ntype)
            return None


class DeprecatedSubmissionBaseUploadForm(SubmissionBaseUploadForm):
    def clean(self):
        def format_messages(where, e, log):
            out = log.write_out.getvalue().splitlines()
            err = log.write_err.getvalue().splitlines()
            m = str(e)
            if m:
                m = [ m ]
            else:
                import traceback
                typ, val, tb = sys.exc_info()
                m = traceback.format_exception(typ, val, tb)
                m = [ l.replace('\n ', ':\n ') for l in m ]
            msgs = [s for s in (["Error from xml2rfc (%s):" % (where,)] + m + out +  err) if s]
            return msgs

        if self.shutdown and not has_role(self.request.user, "Secretariat"):
            raise forms.ValidationError('The submission tool is currently shut down')

        for ext in self.formats:
            f = self.cleaned_data.get(ext, None)
            if not f:
                continue
            self.file_types.append('.%s' % ext)
        if not ('.txt' in self.file_types or '.xml' in self.file_types):
            if not self.errors:
                raise forms.ValidationError('Unexpected submission file types; found %s, but %s is required' % (', '.join(self.file_types), ' or '.join(self.base_formats)))

        #debug.show('self.cleaned_data["xml"]')
        if self.cleaned_data.get('xml'):
            #if not self.cleaned_data.get('txt'):
            xml_file = self.cleaned_data.get('xml')
            file_name = {}
            xml2rfc.log.write_out = io.StringIO()   # open(os.devnull, "w")
            xml2rfc.log.write_err = io.StringIO()   # open(os.devnull, "w")
            tfn = None
            with ExitStack() as stack:
                @stack.callback
                def cleanup():  # called when context exited, even in case of exception
                    if tfn is not None:
                        os.unlink(tfn)

                # We need to write the xml file to disk in order to hand it
                # over to the xml parser.  XXX FIXME: investigate updating
                # xml2rfc to be able to work with file handles to in-memory
                # files.
                name, ext = os.path.splitext(os.path.basename(xml_file.name))
                with tempfile.NamedTemporaryFile(prefix=name+'-',
                                                 suffix='.xml',
                                                 mode='wb+',
                                                 delete=False) as tf:
                    tfn = tf.name
                    for chunk in xml_file.chunks():
                        tf.write(chunk)

                parser = xml2rfc.XmlRfcParser(str(tfn), quiet=True)
                # --- Parse the xml ---
                try:
                    self.xmltree = parser.parse(remove_comments=False)
                    # If we have v2, run it through v2v3. Keep track of the submitted version, though.
                    self.xmlroot = self.xmltree.getroot()
                    self.xml_version = self.xmlroot.get('version', '2')
                    if self.xml_version == '2':
                        v2v3 = xml2rfc.V2v3XmlWriter(self.xmltree)
                        self.xmltree.tree = v2v3.convert2to3()
                        self.xmlroot = self.xmltree.getroot()  # update to the new root

                    draftname = self.xmlroot.attrib.get('docName')
                    if draftname is None:
                        self.add_error('xml', "No docName attribute found in the xml root element")
                    name_error = validate_submission_name(draftname)
                    if name_error:
                        self.add_error('xml', name_error) # This is a critical and immediate failure - do not proceed with other validation.
                    else:
                        revmatch = re.search("-[0-9][0-9]$", draftname)
                        if revmatch:
                            self.revision = draftname[-2:]
                            self.filename = draftname[:-3]
                        else:
                            self.revision = None
                            self.filename = draftname
                        self.title = self.xmlroot.findtext('front/title').strip()
                        if type(self.title) is str:
                            self.title = unidecode(self.title)
                        self.title = normalize_text(self.title)
                        self.abstract = (self.xmlroot.findtext('front/abstract') or '').strip()
                        if type(self.abstract) is str:
                            self.abstract = unidecode(self.abstract)
                        author_info = self.xmlroot.findall('front/author')
                        for author in author_info:
                            info = {
                                "name": author.attrib.get('fullname'),
                                "email": author.findtext('address/email'),
                                "affiliation": author.findtext('organization'),
                            }
                            elem = author.find('address/postal/country')
                            if elem != None:
                                ascii_country = elem.get('ascii', None)
                                info['country'] = ascii_country if ascii_country else elem.text

                            for item in info:
                                if info[item]:
                                    info[item] = info[item].strip()
                            self.authors.append(info)

                        # --- Prep the xml ---
                        file_name['xml'] = os.path.join(settings.IDSUBMIT_STAGING_PATH, '%s-%s%s' % (self.filename, self.revision, ext))
                        try:
                            prep = xml2rfc.PrepToolWriter(self.xmltree, quiet=True, liberal=True, keep_pis=[xml2rfc.V3_PI_TARGET])
                            prep.options.accept_prepped = True
                            self.xmltree.tree = prep.prep()
                            if self.xmltree.tree == None:
                                self.add_error('xml', "Error from xml2rfc (prep): %s" % prep.errors)
                        except Exception as e:
                                msgs = format_messages('prep', e, xml2rfc.log)
                                self.add_error('xml', msgs)

                        # --- Convert to txt ---
                        if not ('txt' in self.cleaned_data and self.cleaned_data['txt']):
                            file_name['txt'] = os.path.join(settings.IDSUBMIT_STAGING_PATH, '%s-%s.txt' % (self.filename, self.revision))
                            try:
                                writer = xml2rfc.TextWriter(self.xmltree, quiet=True)
                                writer.options.accept_prepped = True
                                writer.write(file_name['txt'])
                                log.log("In %s: xml2rfc %s generated %s from %s (version %s)" %
                                        (   os.path.dirname(file_name['xml']),
                                            xml2rfc.__version__,
                                            os.path.basename(file_name['txt']),
                                            os.path.basename(file_name['xml']),
                                            self.xml_version))
                            except Exception as e:
                                msgs = format_messages('txt', e, xml2rfc.log)
                                log.log('\n'.join(msgs))
                                self.add_error('xml', msgs)

                        # --- Convert to html ---
                        try:
                            file_name['html'] = os.path.join(settings.IDSUBMIT_STAGING_PATH, '%s-%s.html' % (self.filename, self.revision))
                            writer = xml2rfc.HtmlWriter(self.xmltree, quiet=True)
                            writer.write(file_name['html'])
                            self.file_types.append('.html')
                            log.log("In %s: xml2rfc %s generated %s from %s (version %s)" %
                                (   os.path.dirname(file_name['xml']),
                                    xml2rfc.__version__,
                                    os.path.basename(file_name['html']),
                                    os.path.basename(file_name['xml']),
                                    self.xml_version))
                        except Exception as e:
                            msgs = format_messages('html', e, xml2rfc.log)
                            self.add_error('xml', msgs)

                except Exception as e:
                    try:
                        msgs = format_messages('txt', e, xml2rfc.log)
                        log.log('\n'.join(msgs))
                        self.add_error('xml', msgs)
                    except Exception:
                        self.add_error('xml', "An exception occurred when trying to process the XML file: %s" % e)

        # The following errors are likely noise if we have previous field
        # errors:
        if self.errors:
            raise forms.ValidationError('')

        if self.cleaned_data.get('txt'):
            # try to parse it
            txt_file = self.cleaned_data['txt']
            txt_file.seek(0)
            bytes = txt_file.read()
            txt_file.seek(0)
            try:
                text = bytes.decode(self.file_info['txt'].charset)
            #
                self.parsed_draft = PlaintextDraft(text, txt_file.name)
                if self.filename == None:
                    self.filename = self.parsed_draft.filename
                elif self.filename != self.parsed_draft.filename:
                    self.add_error('txt', "Inconsistent name information: xml:%s, txt:%s" % (self.filename, self.parsed_draft.filename))
                if self.revision == None:
                    self.revision = self.parsed_draft.revision
                elif self.revision != self.parsed_draft.revision:
                    self.add_error('txt', "Inconsistent revision information: xml:%s, txt:%s" % (self.revision, self.parsed_draft.revision))
                if self.title == None:
                    self.title = self.parsed_draft.get_title()
                elif self.title != self.parsed_draft.get_title():
                    self.add_error('txt', "Inconsistent title information: xml:%s, txt:%s" % (self.title, self.parsed_draft.get_title()))
            except (UnicodeDecodeError, LookupError) as e:
                self.add_error('txt', 'Failed decoding the uploaded file: "%s"' % str(e))

        rev_error = validate_submission_rev(self.filename, self.revision)
        if rev_error:
            raise forms.ValidationError(rev_error)

        # The following errors are likely noise if we have previous field
        # errors:
        if self.errors:
            raise forms.ValidationError('')

        if not self.filename:
            raise forms.ValidationError("Could not extract a valid Internet-Draft name from the upload.  "
                "To fix this in a text upload, please make sure that the full Internet-Draft name including "
                "revision number appears centered on its own line below the document title on the "
                "first page.  In an xml upload, please make sure that the top-level <rfc/> "
                "element has a docName attribute which provides the full Internet-Draft name including "
                "revision number.")

        if not self.revision:
            raise forms.ValidationError("Could not extract a valid Internet-Draft revision from the upload.  "
                "To fix this in a text upload, please make sure that the full Internet-Draft name including "
                "revision number appears centered on its own line below the document title on the "
                "first page.  In an xml upload, please make sure that the top-level <rfc/> "
                "element has a docName attribute which provides the full Internet-Draft name including "
                "revision number.")

        if not self.title:
            raise forms.ValidationError("Could not extract a valid title from the upload")

        if self.cleaned_data.get('txt') or self.cleaned_data.get('xml'):
            # check group
            self.group = self.deduce_group(self.filename)

            # check existing
            existing = Submission.objects.filter(name=self.filename, rev=self.revision).exclude(state__in=("posted", "cancel", "waiting-for-draft"))
            if existing:
                raise forms.ValidationError(mark_safe('A submission with same name and revision is currently being processed. <a href="%s">Check the status here.</a>' % urlreverse("ietf.submit.views.submission_status", kwargs={ 'submission_id': existing[0].pk })))

            # cut-off
            if self.revision == '00' and self.in_first_cut_off:
                raise forms.ValidationError(mark_safe(self.cutoff_warning))

            # check thresholds
            today = date_today()

            self.check_submissions_thresholds(
                "for the Internet-Draft %s" % self.filename,
                dict(name=self.filename, rev=self.revision, submission_date=today),
                settings.IDSUBMIT_MAX_DAILY_SAME_DRAFT_NAME, settings.IDSUBMIT_MAX_DAILY_SAME_DRAFT_NAME_SIZE,
            )
            self.check_submissions_thresholds(
                "for the same submitter",
                dict(remote_ip=self.remote_ip, submission_date=today),
                settings.IDSUBMIT_MAX_DAILY_SAME_SUBMITTER, settings.IDSUBMIT_MAX_DAILY_SAME_SUBMITTER_SIZE,
            )
            if self.group:
                self.check_submissions_thresholds(
                    "for the group \"%s\"" % (self.group.acronym),
                    dict(group=self.group, submission_date=today),
                    settings.IDSUBMIT_MAX_DAILY_SAME_GROUP, settings.IDSUBMIT_MAX_DAILY_SAME_GROUP_SIZE,
                )
            self.check_submissions_thresholds(
                "across all submitters",
                dict(submission_date=today),
                settings.IDSUBMIT_MAX_DAILY_SUBMISSIONS, settings.IDSUBMIT_MAX_DAILY_SUBMISSIONS_SIZE,
            )

        return super().clean()


class SubmissionManualUploadForm(DeprecatedSubmissionBaseUploadForm):
    xml = forms.FileField(label='.xml format', required=False) # xml field with required=False instead of True
    txt = forms.FileField(label='.txt format', required=False)
    # We won't permit html upload until we can verify that the content
    # reasonably matches the text and/or xml upload.  Till then, we generate
    # html for version 3 xml submissions.
    # html = forms.FileField(label='.html format', required=False)
    pdf = forms.FileField(label='.pdf format', required=False)

    def __init__(self, request, *args, **kwargs):
        super(SubmissionManualUploadForm, self).__init__(request, *args, **kwargs)
        self.formats = settings.IDSUBMIT_FILE_TYPES
        self.base_formats = ['txt', 'xml', ]

    def clean_txt(self):
        return self.clean_file("txt", PlainParser)

    def clean_pdf(self):
        return self.clean_file("pdf", PDFParser)

class DeprecatedSubmissionAutoUploadForm(DeprecatedSubmissionBaseUploadForm):
    """Full-service upload form, replaced by the asynchronous version"""
    user = forms.EmailField(required=True)

    def __init__(self, request, *args, **kwargs):
        super(DeprecatedSubmissionAutoUploadForm, self).__init__(request, *args, **kwargs)
        self.formats = ['xml', ]
        self.base_formats = ['xml', ]

class SubmissionAutoUploadForm(SubmissionBaseUploadForm):
    user = forms.EmailField(required=True)
    replaces = forms.CharField(required=False, max_length=1000, strip=True)

    def __init__(self, request, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.formats = ['xml', ]
        self.base_formats = ['xml', ]

    def clean(self):
        super().clean()

        # Clean the replaces field after the rest of the cleaning so we know the name of the
        # uploaded draft via self.filename
        if self.cleaned_data['replaces']:
            names_replaced = [s.strip() for s in self.cleaned_data['replaces'].split(',')]
            self.cleaned_data['replaces'] = ','.join(names_replaced)
            aliases_replaced = DocAlias.objects.filter(name__in=names_replaced)
            if len(names_replaced) != len(aliases_replaced):
                known_names = aliases_replaced.values_list('name', flat=True)
                unknown_names = [n for n in names_replaced if n not in known_names]
                self.add_error(
                    'replaces',
                    forms.ValidationError(
                        'Unknown Internet-Draft name(s): ' + ', '.join(unknown_names)
                    ),
                )
            for alias in aliases_replaced:
                if alias.document.name == self.filename:
                    self.add_error(
                        'replaces',
                        forms.ValidationError("An Internet-Draft cannot replace itself"),
                    )
                elif alias.document.type_id != "draft":
                    self.add_error(
                        'replaces',
                        forms.ValidationError("An Internet-Draft can only replace another Internet-Draft"),
                    )
                elif alias.document.get_state_slug() == "rfc":
                    self.add_error(
                        'replaces',
                        forms.ValidationError("An Internet-Draft cannot replace an RFC"),
                    )
                elif alias.document.get_state_slug('draft-iesg') in ('approved', 'ann', 'rfcqueue'):
                    self.add_error(
                        'replaces',
                        forms.ValidationError(
                            alias.name + " is approved by the IESG and cannot be replaced"
                        ),
                    )


class NameEmailForm(forms.Form):
    name = forms.CharField(required=True)
    email = forms.EmailField(label='Email address', required=True)

    def __init__(self, *args, **kwargs):
        super(NameEmailForm, self).__init__(*args, **kwargs)

        self.fields["name"].widget.attrs["class"] = "name"
        self.fields["email"].widget.attrs["class"] = "email"

    def clean_name(self):
        return self.cleaned_data["name"].replace("\n", "").replace("\r", "").replace("<", "").replace(">", "").strip()

    def clean_email(self):
        return self.cleaned_data["email"].replace("\n", "").replace("\r", "").replace("<", "").replace(">", "").strip()

class AuthorForm(NameEmailForm):
    affiliation = forms.CharField(max_length=100, required=False)
    country = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super(AuthorForm, self).__init__(*args, **kwargs)

class SubmitterForm(NameEmailForm):
    #Fields for secretariat only
    approvals_received = forms.BooleanField(label='Approvals received', required=False, initial=False)

    def cleaned_line(self):
        line = self.cleaned_data["name"]
        email = self.cleaned_data.get("email")
        if email:
            line = formataddr((line, email))
        return line

class ReplacesForm(forms.Form):
    replaces = SearchableDocAliasesField(required=False, help_text="Any Internet-Drafts that this document replaces (approval required for replacing an Internet-Draft you are not the author of)")

    def __init__(self, *args, **kwargs):
        self.name = kwargs.pop("name")
        super(ReplacesForm, self).__init__(*args, **kwargs)

    def clean_replaces(self):
        for alias in self.cleaned_data['replaces']:
            if alias.document.name == self.name:
                raise forms.ValidationError("An Internet-Draft cannot replace itself.")
            if alias.document.type_id != "draft":
                raise forms.ValidationError("An Internet-Draft can only replace another Internet-Draft")
            if alias.document.get_state_slug() == "rfc":
                raise forms.ValidationError("An Internet-Draft cannot replace an RFC")
            if alias.document.get_state_slug('draft-iesg') in ('approved','ann','rfcqueue'):
                raise forms.ValidationError(alias.name+" is approved by the IESG and cannot be replaced")
        return self.cleaned_data['replaces']

class EditSubmissionForm(forms.ModelForm):
    title = forms.CharField(required=True, max_length=255)
    rev = forms.CharField(label='Revision', max_length=2, required=True)
    document_date = forms.DateField(required=True)
    pages = forms.IntegerField(required=True)
    formal_languages = forms.ModelMultipleChoiceField(queryset=FormalLanguageName.objects.filter(used=True), widget=forms.CheckboxSelectMultiple, required=False)
    abstract = forms.CharField(widget=forms.Textarea, required=True, strip=False)

    note = forms.CharField(label=mark_safe('Comment to the Secretariat'), widget=forms.Textarea, required=False, strip=False)

    class Meta:
        model = Submission
        fields = ['title', 'rev', 'document_date', 'pages', 'formal_languages', 'abstract', 'note']

    def clean_rev(self):
        rev = self.cleaned_data["rev"]

        if len(rev) == 1:
            rev = "0" + rev

        error = validate_submission_rev(self.instance.name, rev)
        if error:
            raise forms.ValidationError(error)

        return rev

    def clean_document_date(self):
        document_date = self.cleaned_data['document_date']
        error = validate_submission_document_date(self.instance.submission_date, document_date)
        if error:
            raise forms.ValidationError(error)

        return document_date

class PreapprovalForm(forms.Form):
    name = forms.CharField(max_length=255, required=True, label="Pre-approved name", initial="draft-")

    def clean_name(self):
        n = self.cleaned_data['name'].strip().lower()
        error_msg = validate_submission_name(n)
        if error_msg:
            raise forms.ValidationError(error_msg)

        components = n.split("-")
        if components[-1] == "00":
            raise forms.ValidationError("Name appears to end with a revision number -00 - do not include the revision.")
        if len(components) < 4:
            raise forms.ValidationError("Name has less than four dash-delimited components - can't form a valid group Internet-Draft name.")
        acronym = components[2]
        if acronym not in [ g.acronym for g in self.groups ]:
            raise forms.ValidationError("Group acronym not recognized as one you can approve Internet-Drafts for.")

        if Preapproval.objects.filter(name=n):
            raise forms.ValidationError("Pre-approval for this name already exists.")
        if Submission.objects.filter(state="posted", name=n):
            raise forms.ValidationError("An Internet-Draft with this name has already been submitted and accepted. A pre-approval would not make any difference.")

        return n


class SubmissionEmailForm(forms.Form):
    '''
    Used to add a message to a submission or to create a new submission.
    This message is NOT a reply to a previous message but has arrived out of band
    
    if submission_pk is None we are starting a new submission and name
    must be unique. Otherwise the name must match the submission.name.
    '''
    name = forms.CharField(required=True, max_length=255, label="Internet-Draft name")
    submission_pk = forms.IntegerField(required=False, widget=forms.HiddenInput())
    direction = forms.ChoiceField(choices=(("incoming", "Incoming"), ("outgoing", "Outgoing")),
                                  widget=forms.RadioSelect)
    message = forms.CharField(required=True, widget=forms.Textarea, strip=False,
                              help_text="Copy the entire message including headers. To do so, view the source, select all, copy then paste into the text area above")
    #in_reply_to = MessageModelChoiceField(queryset=Message.objects,label="In Reply To",required=False)

    def __init__(self, *args, **kwargs):
        super(SubmissionEmailForm, self).__init__(*args, **kwargs)

    def clean_message(self):
        '''Returns a ietf.message.models.Message object'''
        self.message_text = self.cleaned_data['message']
        try:
            message = email.message_from_string(force_str(self.message_text))
        except Exception as e:
            self.add_error('message', e)
            return None
            
        for field in ('to','from','subject','date'):
            if not message[field]:
                raise forms.ValidationError('Error parsing email: {} field not found.'.format(field))
        date = utc_from_string(message['date'])
        if not isinstance(date,datetime.datetime):
            raise forms.ValidationError('Error parsing email date field')
        return message

    def clean(self):
        if any(self.errors):
            return self.cleaned_data
        super(SubmissionEmailForm, self).clean()
        name = self.cleaned_data['name']
        match = re.search(r"(draft-[a-z0-9-]*)-(\d\d)", name)
        if not match:
            self.add_error('name', 
                           "Submission name {} must start with 'draft-' and only contain digits, lowercase letters and dash characters and end with revision.".format(name))
        else:
            self.draft_name = match.group(1)    
            self.revision = match.group(2)

            error = validate_submission_rev(self.draft_name, self.revision)
            if error:
                raise forms.ValidationError(error)

        #in_reply_to = self.cleaned_data['in_reply_to']
        #message = self.cleaned_data['message']
        direction = self.cleaned_data['direction']
        if direction != 'incoming' and direction != 'outgoing':
            self.add_error('direction', "Must be one of 'outgoing' or 'incoming'")

        #if in_reply_to:
        #    if direction != 'incoming':
        #        raise forms.ValidationError('Only incoming messages can have In Reply To selected')
        #    date = utc_from_string(message['date'])
        #    if date < in_reply_to.time:
        #        raise forms.ValidationError('The incoming message must have a date later than the message it is replying to')

        return self.cleaned_data

class MessageModelForm(forms.ModelForm):
    in_reply_to_id = forms.CharField(required=False, widget=forms.HiddenInput())
    
    class Meta:
        model = Message
        fields = ['to','frm','cc','bcc','reply_to','subject','body']
        exclude = ['time','by','content_type','related_groups','related_docs']

    def __init__(self, *args, **kwargs):
        super(MessageModelForm, self).__init__(*args, **kwargs)
        self.fields['frm'].label='From'
        self.fields['frm'].widget.attrs['readonly'] = True
        self.fields['reply_to'].widget.attrs['readonly'] = True
