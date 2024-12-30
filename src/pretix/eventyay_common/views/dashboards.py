from datetime import timedelta

import pytz
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    Count, Exists, IntegerField, Max, Min, OuterRef, Q, Subquery,
)
from django.db.models.functions import Coalesce, Greatest
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.html import escape
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _, pgettext

from pretix.base.models import (
    Item, ItemCategory, Order, OrderRefund, Question, Quota, RequiredAction,
    SubEvent, Voucher,
)
from pretix.base.models.orders import CancellationRequest
from pretix.base.timeline import timeline_for_event
from pretix.control.forms.event import CommentForm
from pretix.control.logdisplay import OVERVIEW_BANLIST
from pretix.control.signals import (
    event_dashboard_widgets, user_dashboard_widgets,
)
from pretix.helpers.daterange import daterange
from pretix.helpers.plugin_enable import is_video_enabled

from .event import EventCreatedFor


def organiser_dashboard(request):
    widgets = []
    for r, result in user_dashboard_widgets.send(request, user=request.user):
        widgets.extend(result)

    ctx = {
        'widgets': rearrange(widgets),
        'can_create_event': request.user.teams.filter(
            can_create_events=True
        ).exists(),
        'upcoming': widgets_for_event_qs(
            request,
            annotated_event_query(request, lazy=True)
            .filter(
                Q(has_subevents=False)
                & Q(
                    Q(Q(date_to__isnull=True) & Q(date_from__gte=now()))
                    | Q(Q(date_to__isnull=False) & Q(date_to__gte=now()))
                )
            ).order_by('date_from', 'order_to', 'pk'),
            request.user,
            7,
            lazy=True,
        ),
        'past': widgets_for_event_qs(
            request,
            annotated_event_query(request, lazy=True)
            .filter(
                Q(has_subevents=False)
                & Q(
                    Q(Q(date_to__isnull=True) & Q(date_from__lt=now()))
                    | Q(Q(date_to__isnull=False) & Q(date_to__lt=now()))
                )
            ).order_by('-order_to', 'pk'),
            request.user,
            8,
            lazy=True,
        ),
        'series': widgets_for_event_qs(
            request,
            annotated_event_query(request, lazy=True).filter(
                has_subevents=True
            ).order_by('-order_to', 'pk'),
            request.user,
            8,
            lazy=True
        ),
        'ticket_component': f'{settings.SITE_URL}/control',
        'talk_component': f'{settings.TALK_HOSTNAME}/orga',
        'video_component': '#',
    }

    return render(request, 'eventyay_common/dashboard/dashboard.html', ctx)


def rearrange(widgets: list):
    """
    Sort widget boxes according to priority.
    """
    mapping = {
        'small': 1,
        'big': 2,
        'full': 3,
    }

    def sort_key(element):
        return (
            element.get('priority', 1),
            mapping.get(element.get('display_size', 'small'), 1),
        )

    return sorted(widgets, key=sort_key, reverse=True)


def widgets_for_event_qs(request, qs, user, nmax, lazy=False):
    widgets = []

    tpl = """
        <a href="{url}" class="event">
            <div class="name">{event}</div>
            <div class="daterange">{daterange}</div>
            <div class="times">{times}</div>
        </a>
        <div class="bottomrow">
            <a href="{ticket_url}" class="component">
                Ticket
            </a>
            {talk_button}
            {video_button}
        </div>
    """

    if lazy:
        events = qs[:nmax]
    else:
        events = qs.prefetch_related(
            '_settings_objects', 'organizer___settings_objects'
        ).select_related('organizer')[:nmax]

    for event in events:
        dr = None
        status = None

        if not lazy:
            tzname = event.cache.get_or_set('timezone', lambda e=event: e.settings.timezone)
            tz = pytz.timezone(tzname)
            if event.has_subevents:
                dr = pgettext("subevent", "No dates") if event.min_from is None else daterange(
                        (event.min_from).astimezone(tz),
                        (event.max_fromto or event.max_to or event.max_from).astimezone(tz)
                    )
            elif event.date_to:
                dr = daterange(event.date_from.astimezone(tz), event.date_to.astimezone(tz))
            else:
                dr = date_format(event.date_from.astimezone(tz), "DATE_FORMAT")

            if event.has_ra:
                status = ('danger', _('Action required'))
            elif not event.live:
                status = ('warning', _('Shop disabled'))
            elif event.presale_has_ended:
                status = ('default', _('Sale over'))
            elif not event.presale_is_running:
                status = ('default', _('Soon'))
            else:
                status = ('success', _('On sale'))

        if is_video_enabled(event):
            url = reverse(
                'eventyay_common:event.create_access_to_video',
                kwargs={
                    'event': event.slug,
                    'organizer': event.organizer.slug
                }
            )
            video_button = f"""
            <a href={url}" class="component">{_('Video')}</a>
            """
        else:
            video_button = f"""
                <a href="#" data-toggle="modal" data-target="#alert-modal" class="component">
                    {_('Video')}
                </a>
                <div class="modal fade" id="alert-modal" tabindex="-1" role="dialog">
                    <div class="modal-dialog modal-dialog-centered" role="document">
                        <div class="modal-content">
                            <div class="modal-header bg-danger">
                                <h3 class="modal-title text-center text-danger">{_('Alert')}</h3>
                            </div>
                            <div class="modal-body">
                                <div class="text-center">
                                    <p>
                                        You need to enable this component first.
                                    </p>
                                </div>
                            </div>
                            <div class="modal-footer">
                                <button type="button" class="btn btn-secondary" data-dismiss="modal">{_('Close')}</button>
                            </div>
                        </div>
                    </div>
                </div>
            """
        if (
            event.settings.create_for == EventCreatedFor.BOTH.value
            or event.settings.talk_schedule_public is not None
        ):
            talk_url = f'{settings.TALK_HOSTNAME}orga/event/{event.slug}'
            talk_button = f"""<a href="{talk_url}" class="middle-component">{_('Talk')}</a>"""
        else:
            talk_button = f"""
                <a href="#" data-toggle="modal" data-target="#alert-modal" class="middle-component">
                    {_('Talk')}
                </a>
                <div class="modal fade" id="alert-modal" tabindex="-1" role="dialog">
                    <div class="modal-dialog modal-dialog-centered" role="document">
                        <div class="modal-content">
                            <div class="modal-header bg-danger">
                                <h3 class="modal-title text-center text-danger">{_('Alert')}</h3>
                            </div>
                            <div class="modal-body">
                                <div class="text-center">
                                    <p>
                                        You need to enable this component first.
                                    </p>
                                </div>
                            </div>
                            <div class="modal-footer">
                                <button type="button" class="btn btn-secondary" data-dismiss="modal">{_('Close')}</button>
                            </div>
                        </div>
                    </div>
                </div>
            """

        widgets.append({
            'content': tpl.format(
                event=escape(event.name),
                times=_('Event series') if event.has_subevents else (
                    ((date_format(event.date_admission.astimezone(tz), 'TIME_FORMAT') + ' / ')
                     if event.date_admission and event.date_admission != event.date_from else '')
                    + (date_format(event.date_from.astimezone(tz), 'TIME_FORMAT') if event.date_from else '')
                ) + (
                    ' <span class="fa fa-globe text-muted" data-toggle="tooltip" title="{}"></span>'.format(tzname)
                    if tzname != request.timezone and not event.has_subevents else ''
                ),
                url=reverse(
                    "eventyay_common:event.update",
                    kwargs={
                        "organizer": event.organizer.slug,
                        "event": event.slug,
                    }
                ),
                daterange=dr,
                status=status[1],
                statusclass=status[0],
                ticket_url=reverse(
                    'control:event.index',
                    kwargs={
                        'event': event.slug,
                        'organizer': event.organizer.slug
                    }
                ),
                video_button=video_button,
                talk_button=talk_button
            ) if not lazy else '',
            'display_size': 'small',
            'lazy': f'event-{event.pk}',
            'priority': 100,
            'container_class': 'widget-container widget-container-event',
        })
        """
            {% if not e.live %}
                <span class="label label-danger">{% trans "Shop disabled" %}</span>
            {% elif e.presale_has_ended %}
                <span class="label label-warning">{% trans "Presale over" %}</span>
            {% elif not e.presale_is_running %}
                <span class="label label-warning">{% trans "Presale not started" %}</span>
            {% else %}
                <span class="label label-success">{% trans "On sale" %}</span>
            {% endif %}
        """
    return widgets


def annotated_event_query(request, lazy=False):
    active_orders = Order.objects.filter(
        event=OuterRef('pk'),
        status__in=[Order.STATUS_PENDING, Order.STATUS_PAID]
    ).order_by().values('event').annotate(
        c=Count('*')
    ).values(
        'c'
    )

    required_actions = RequiredAction.objects.filter(
        event=OuterRef('pk'),
        done=False
    )
    qs = request.user.get_events_with_any_permission(request)
    if not lazy:
        qs = qs.annotate(
            order_count=Subquery(active_orders, output_field=IntegerField()),
            has_ra=Exists(required_actions)
        )
    qs = qs.annotate(
        min_from=Min('subevents__date_from'),
        max_from=Max('subevents__date_from'),
        max_to=Max('subevents__date_to'),
        max_fromto=Greatest(Max('subevents__date_to'), Max('subevents__date_from')),
    ).annotate(
        order_to=Coalesce('max_fromto', 'max_to', 'max_from', 'date_to', 'date_from'),
    )
    return qs


def user_index_widgets_lazy(request):
    widgets = []
    widgets += widgets_for_event_qs(
        request,
        annotated_event_query(request).filter(
            Q(has_subevents=False) &
            Q(
                Q(Q(date_to__isnull=True) & Q(date_from__gte=now()))
                | Q(Q(date_to__isnull=False) & Q(date_to__gte=now()))
            )
        ).order_by('date_from', 'order_to', 'pk'),
        request.user,
        7
    )
    widgets += widgets_for_event_qs(
        request,
        annotated_event_query(request).filter(
            Q(has_subevents=False) &
            Q(
                Q(Q(date_to__isnull=True) & Q(date_from__lt=now()))
                | Q(Q(date_to__isnull=False) & Q(date_to__lt=now()))
            )
        ).order_by('-order_to', 'pk'),
        request.user,
        8
    )
    widgets += widgets_for_event_qs(
        request,
        annotated_event_query(request).filter(
            has_subevents=True
        ).order_by('-order_to', 'pk'),
        request.user,
        8
    )
    return JsonResponse({'widgets': widgets})


def event_index(request, organizer, event):
    subevent = None
    if request.GET.get("subevent", "") != "" and request.event.has_subevents:
        i = request.GET.get("subevent", "")
        try:
            subevent = request.event.subevents.get(pk=i)
        except SubEvent.DoesNotExist:
            pass

    can_view_orders = request.user.has_event_permission(request.organizer, request.event, 'can_view_orders',
                                                        request=request)
    can_change_orders = request.user.has_event_permission(request.organizer, request.event, 'can_change_orders',
                                                          request=request)
    can_change_event_settings = request.user.has_event_permission(request.organizer, request.event,
                                                                  'can_change_event_settings', request=request)
    can_view_vouchers = request.user.has_event_permission(request.organizer, request.event, 'can_view_vouchers',
                                                          request=request)

    widgets = []
    if can_view_orders:
        for r, result in event_dashboard_widgets.send(sender=request.event, subevent=subevent, lazy=True):
            widgets.extend(result)

    qs = request.event.logentry_set.all().select_related('user', 'content_type', 'api_token', 'oauth_application',
                                                         'device').order_by('-datetime')
    qs = qs.exclude(action_type__in=OVERVIEW_BANLIST)
    if not can_view_orders:
        qs = qs.exclude(content_type=ContentType.objects.get_for_model(Order))
    if not can_view_vouchers:
        qs = qs.exclude(content_type=ContentType.objects.get_for_model(Voucher))
    if not can_change_event_settings:
        allowed_types = [
            ContentType.objects.get_for_model(Voucher),
            ContentType.objects.get_for_model(Order)
        ]
        if request.user.has_event_permission(request.organizer, request.event, 'can_change_items', request=request):
            allowed_types += [
                ContentType.objects.get_for_model(Item),
                ContentType.objects.get_for_model(ItemCategory),
                ContentType.objects.get_for_model(Quota),
                ContentType.objects.get_for_model(Question),
            ]
        qs = qs.filter(content_type__in=allowed_types)

    a_qs = request.event.requiredaction_set.filter(done=False)

    ctx = {
        'widgets': rearrange(widgets),
        'logs': qs[:5],
        'subevent': subevent,
        'actions': a_qs[:5] if can_change_orders else [],
        'comment_form': CommentForm(initial={'comment': request.event.comment}, readonly=not can_change_event_settings),
    }

    ctx['has_overpaid_orders'] = can_view_orders and Order.annotate_overpayments(request.event.orders).filter(
        Q(~Q(status=Order.STATUS_CANCELED) & Q(pending_sum_t__lt=0))
        | Q(Q(status=Order.STATUS_CANCELED) & Q(pending_sum_rc__lt=0))
    ).exists()
    ctx['has_pending_orders_with_full_payment'] = can_view_orders and Order.annotate_overpayments(request.event.orders).filter(
        Q(status__in=(Order.STATUS_EXPIRED, Order.STATUS_PENDING)) & Q(pending_sum_t__lte=0) & Q(require_approval=False)
    ).exists()
    ctx['has_pending_refunds'] = can_view_orders and OrderRefund.objects.filter(
        order__event=request.event,
        state__in=(OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_EXTERNAL)
    ).exists()
    ctx['has_pending_approvals'] = can_view_orders and request.event.orders.filter(
        status=Order.STATUS_PENDING,
        require_approval=True
    ).exists()
    ctx['has_cancellation_requests'] = can_view_orders and CancellationRequest.objects.filter(
        order__event=request.event
    ).exists()

    for a in ctx['actions']:
        a.display = a.display(request)

    ctx['timeline'] = [
        {
            'date': t.datetime.astimezone(request.event.timezone).date(),
            'entry': t,
            'time': t.datetime.astimezone(request.event.timezone)
        }
        for t in timeline_for_event(request.event, subevent)
    ]
    ctx['today'] = now().astimezone(request.event.timezone).date()
    ctx['nearly_now'] = now().astimezone(request.event.timezone) - timedelta(seconds=20)
    resp = render(request, 'pretixcontrol/event/index.html', ctx)
    # resp['Content-Security-Policy'] = "style-src 'unsafe-inline'"
    return resp


def event_index_widgets_lazy(request, organizer, event):
    subevent = None
    if request.GET.get("subevent", "") != "" and request.event.has_subevents:
        i = request.GET.get("subevent", "")
        try:
            subevent = request.event.subevents.get(pk=i)
        except SubEvent.DoesNotExist:
            pass

    widgets = []
    for r, result in event_dashboard_widgets.send(sender=request.event, subevent=subevent, lazy=False):
        widgets.extend(result)

    return JsonResponse({'widgets': widgets})
