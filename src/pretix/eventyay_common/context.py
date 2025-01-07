import logging
from urllib.parse import urljoin

from django.conf import settings
from django.db.models import Q
from django.http import HttpRequest
from django.urls import Resolver404, get_script_prefix, resolve
from django_scopes import scope

from pretix.base.models.auth import StaffSession
from pretix.base.settings import GlobalSettingsObject
from pretix.eventyay_common.navigation import (
    get_event_navigation, get_global_navigation,
)

from ..helpers.plugin_enable import is_video_enabled
from ..multidomain.urlreverse import get_event_domain
from .views.event import EventCreatedFor

logger = logging.getLogger(__name__)


def contextprocessor(request: HttpRequest):
    if not hasattr(request, "_eventyay_common_default_context"):
        request._eventyay_common_default_context = _default_context(request)
    return request._eventyay_common_default_context


def _default_context(request: HttpRequest):
    try:
        url = resolve(request.path_info)
    except Resolver404:
        return {}

    if not request.path.startswith(f"{get_script_prefix()}common"):
        return {}
    ctx = {
        "url_name": url.url_name,
        "settings": settings,
        "django_settings": settings,
        "DEBUG": settings.DEBUG,
        "talk_hostname": settings.TALK_HOSTNAME,
    }

    gs = GlobalSettingsObject()
    ctx["global_settings"] = gs.settings

    if request.user.is_authenticated:
        ctx["nav_items"] = get_global_navigation(request)

        ctx["staff_session"] = request.user.has_active_staff_session(
            request.session.session_key
        )
        ctx["staff_need_to_explain"] = (
            StaffSession.objects.filter(
                user=request.user, date_end__isnull=False
            ).filter(Q(comment__isnull=True) | Q(comment=""))
            if request.user.is_staff and settings.PRETIX_ADMIN_AUDIT_COMMENTS
            else StaffSession.objects.none()
        )

        if event := getattr(request, "event", None):
            ctx["talk_edit_url"] = urljoin(
                settings.TALK_HOSTNAME, f"/orga/event/{request.event.slug}"
            )
            ctx["is_video_enabled"] = is_video_enabled(event)
            ctx["is_talk_event_created"] = False
            if (
                request.event.settings.create_for == EventCreatedFor.BOTH.value
                or request.event.settings.talk_schedule_public is not None
            ):
                ctx["is_talk_event_created"] = True

            if organizer := getattr(request, "organizer", None):
                ctx["nav_items"] = get_event_navigation(request)
                ctx["has_domain"] = (
                    get_event_domain(request.event, fallback=True) is not None
                )
                if not request.event.testmode:
                    with scope(organizer=organizer):
                        complain_testmode_orders = request.event.cache.get(
                            "complain_testmode_orders"
                        )
                        if complain_testmode_orders is None:
                            complain_testmode_orders = request.event.orders.filter(
                                testmode=True
                            ).exists()
                            request.event.cache.set(
                                "complain_testmode_orders", complain_testmode_orders, 30
                            )
                    ctx["complain_testmode_orders"] = (
                        complain_testmode_orders
                        and request.user.has_event_permission(
                            organizer, request.event, "can_view_orders", request=request
                        )
                    )
                else:
                    ctx["complain_testmode_orders"] = False

                if not request.event.live and ctx["has_domain"]:
                    child_sess_key = f"child_session_{request.event.pk}"
                    child_sess = request.session.get(child_sess_key)

                    if not child_sess:
                        request.session[child_sess_key] = request.session.session_key
                    else:
                        ctx["new_session"] = child_sess
                    request.session["event_access"] = True
                if request.GET.get("subevent", ""):
                    subevent_id = request.GET.get("subevent", "").strip()
                    try:
                        pk = int(subevent_id)
                        # Do not use .get() for lazy evaluation
                        ctx["selected_subevents"] = request.event.subevents.filter(
                            pk=pk
                        )
                    except ValueError as e:
                        logger.error("Error parsing subevent ID: %s", e)

    return ctx
