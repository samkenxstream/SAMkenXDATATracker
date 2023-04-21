# Copyright The IETF Trust 2012-2020, All Rights Reserved
# -*- coding: utf-8 -*-


import datetime
import subprocess
import os
import json

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from ietf.doc.models import DeletedEvent, StateDocEvent, DocEvent
from ietf.ietfauth.utils import role_required, has_role
from ietf.sync.discrepancies import find_discrepancies
from ietf.utils.serialize import object_as_shallow_dict
from ietf.utils.log import log
from ietf.utils.response import permission_denied


SYNC_BIN_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../bin"))

#@role_required('Secretariat', 'IANA', 'RFC Editor')
def discrepancies(request):
    sections = find_discrepancies()

    return render(request, "sync/discrepancies.html", dict(sections=sections))

@csrf_exempt # external API so we can't expect the other end to have a token
def notify(request, org, notification):
    """Notify us that something has changed on an external site so we need to
    run a sync script."""

    known_orgs = {
        "iana": "IANA",
        "rfceditor": "RFC Editor",
        }

    if org not in known_orgs:
        raise Http404

    # handle auth, to make it easier for the other end, you can send
    # the username/password as POST parameters instead of having to
    # visit the login page
    user = request.user

    username = request.POST.get("username") or request.GET.get("username")
    password = request.POST.get("password") or request.GET.get("password")

    if username != None and password != None:
        if settings.SERVER_MODE == "production" and not request.is_secure():
            permission_denied(request, "You must use HTTPS when sending username/password.")

        if not user.is_authenticated:
            try:
                user = User.objects.get(username__iexact=username)
            except User.DoesNotExist:
                return HttpResponse("Invalid username/password")

            if not user.check_password(password):
                return HttpResponse("Invalid username/password")

    if not has_role(user, ("Secretariat", known_orgs[org])):
        permission_denied(request, "You do not have the necessary permissions to view this page.")

    known_notifications = {
        "protocols": "an added reference to an RFC at <a href=\"%s\">the IANA protocols page</a>" % settings.IANA_SYNC_PROTOCOLS_URL,
        "changes": "new changes at <a href=\"%s\">the changes JSON dump</a>" % settings.IANA_SYNC_CHANGES_URL,
        "queue": "new changes to <a href=\"%s\">queue2.xml</a>" % settings.RFC_EDITOR_QUEUE_URL,
        "index": "new changes to <a href=\"%s\">rfc-index.xml</a>" % settings.RFC_EDITOR_INDEX_URL,
        }

    if notification not in known_notifications:
        raise Http404

    if request.method == "POST":
        def runscript(name):
            python = os.path.join(os.path.dirname(settings.BASE_DIR), "env", "bin", "python")
            cmd = [python, os.path.join(SYNC_BIN_PATH, name)]
            cmdstring = " ".join(cmd)
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate()
            out = out.decode('utf-8')
            err = err.decode('utf-8')
            if p.returncode:
                log("Subprocess error %s when running '%s': %s %s" % (p.returncode, cmd, err, out))
                raise subprocess.CalledProcessError(p.returncode, cmdstring, "\n".join([err, out]))

        log("Running sync script from notify view POST")

        if notification == "protocols":
            runscript("iana-protocols-updates")

        if notification == "changes":
            runscript("iana-changes-updates")

        if notification == "queue":
            runscript("rfc-editor-queue-updates")

        if notification == "index":
            runscript("rfc-editor-index-updates")

        return HttpResponse("OK", content_type="text/plain; charset=%s"%settings.DEFAULT_CHARSET)

    return render(request, 'sync/notify.html',
                  dict(org=known_orgs[org],
                       notification=notification,
                       help_text=known_notifications[notification],
                  ))

@role_required('Secretariat', 'RFC Editor')
def rfceditor_undo(request):
    """Undo a DocEvent."""
    events = []
    events.extend(StateDocEvent.objects.filter(
        state_type="draft-rfceditor",
        time__gte=timezone.now() - datetime.timedelta(weeks=1)
    ).order_by("-time", "-id"))

    events.extend(DocEvent.objects.filter(
        type="sync_from_rfc_editor",
        time__gte=timezone.now() - datetime.timedelta(weeks=1)
    ).order_by("-time", "-id"))

    events.sort(key=lambda e: (e.time, e.id), reverse=True)

    if request.method == "POST":
        try:
            eid = int(request.POST.get("event", ""))
        except ValueError:
            return HttpResponse("Could not parse event id")

        for e in events:
            if e.id == eid:
                break
        else:
            return HttpResponse("Event does not exist")

        doc = e.doc

        # possibly reset the state of the document
        all_events = StateDocEvent.objects.filter(doc=doc, state_type="draft-rfceditor").order_by("-time", "-id")
        if all_events and all_events[0] == e:
            if len(all_events) > 1:
                doc.set_state(all_events[1].state)
            else:
                doc.unset_state("draft-rfceditor")

        dump = DeletedEvent()
        dump.content_type = ContentType.objects.get_for_model(type(e))
        dump.json = json.dumps(object_as_shallow_dict(e), indent=2)
        dump.by = request.user.person
        dump.save()

        e.delete()

        return HttpResponseRedirect("")

    return render(request, 'sync/rfceditor_undo.html', dict(events=events))
