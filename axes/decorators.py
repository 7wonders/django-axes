import logging
import socket

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.utils import timezone as datetime
from django.utils.translation import ugettext_lazy

try:
    from django.contrib.auth import get_user_model
except ImportError:  # django < 1.5
    from django.contrib.auth.models import User
else:
    User = get_user_model()

try:
    from django.contrib.auth.models import SiteProfileNotAvailable
except ImportError: # django >= 1.7
    SiteProfileNotAvailable = type('SiteProfileNotAvailable', (Exception,), {})

from axes.models import AccessLog
from axes.models import AccessAttempt
from axes.signals import user_locked_out
import axes
from django.utils import six


# see if the user has overridden the failure limit
FAILURE_LIMIT = getattr(settings, 'AXES_LOGIN_FAILURE_LIMIT', 3)

# see if the user has set axes to lock out logins after failure limit
LOCK_OUT_AT_FAILURE = getattr(settings, 'AXES_LOCK_OUT_AT_FAILURE', True)

USE_USER_AGENT = getattr(settings, 'AXES_USE_USER_AGENT', False)

# see if the django app is sitting behind a reverse proxy
BEHIND_REVERSE_PROXY = getattr(settings, 'AXES_BEHIND_REVERSE_PROXY', False)

# see if the django app is sitting behind a reverse proxy but can be accessed directly
BEHIND_REVERSE_PROXY_WITH_DIRECT_ACCESS = getattr(settings, 'AXES_BEHIND_REVERSE_PROXY_WITH_DIRECT_ACCESS', False)

# if the django app is behind a reverse proxy, look for the ip address using this HTTP header value
REVERSE_PROXY_HEADER = getattr(settings, 'AXES_REVERSE_PROXY_HEADER', 'HTTP_X_FORWARDED_FOR')

COOLOFF_TIME = getattr(settings, 'AXES_COOLOFF_TIME', None)
if isinstance(COOLOFF_TIME, int):
    COOLOFF_TIME = timedelta(hours=COOLOFF_TIME)

LOGGER = getattr(settings, 'AXES_LOGGER', 'axes.watch_login')

LOCKOUT_TEMPLATE = getattr(settings, 'AXES_LOCKOUT_TEMPLATE', None)
VERBOSE = getattr(settings, 'AXES_VERBOSE', True)

# whitelist and blacklist
# todo: convert the strings to IPv4 on startup to avoid type conversion during processing
ONLY_WHITELIST = getattr(settings, 'AXES_ONLY_ALLOW_WHITELIST', False)
IP_WHITELIST = getattr(settings, 'AXES_IP_WHITELIST', None)
IP_BLACKLIST = getattr(settings, 'AXES_IP_BLACKLIST', None)

ERROR_MESSAGE = ugettext_lazy("Please enter a correct username and password. "
                              "Note that both fields are case-sensitive.")


log = logging.getLogger(LOGGER)
if VERBOSE:
    log.info('AXES: BEGIN LOG')
    log.info('Using django-axes ' + axes.get_version())


if BEHIND_REVERSE_PROXY:
    log.debug('Axes is configured to be behind reverse proxy...looking for header value %s', REVERSE_PROXY_HEADER)


def is_valid_ip(ip_address):
    """ Check Validity of an IP address """
    valid = True
    try:
        socket.inet_aton(ip_address.strip())
    except:
        valid = False
    return valid


def get_ip_address_from_request(request):
    """ Makes the best attempt to get the client's real IP or return the loopback """
    PRIVATE_IPS_PREFIX = ('10.', '172.', '192.', '127.')
    ip_address = ''
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if x_forwarded_for and ',' not in x_forwarded_for:
        if not x_forwarded_for.startswith(PRIVATE_IPS_PREFIX) and is_valid_ip(x_forwarded_for):
            ip_address = x_forwarded_for.strip()
    else:
        ips = [ip.strip() for ip in x_forwarded_for.split(',')]
        for ip in ips:
            if ip.startswith(PRIVATE_IPS_PREFIX):
                continue
            elif not is_valid_ip(ip):
                continue
            else:
                ip_address = ip
                break
    if not ip_address:
        x_real_ip = request.META.get('HTTP_X_REAL_IP', '')
        if x_real_ip:
            if not x_real_ip.startswith(PRIVATE_IPS_PREFIX) and is_valid_ip(x_real_ip):
                ip_address = x_real_ip.strip()
    if not ip_address:
        remote_addr = request.META.get('REMOTE_ADDR', '')
        if remote_addr:
            if not remote_addr.startswith(PRIVATE_IPS_PREFIX) and is_valid_ip(remote_addr):
                ip_address = remote_addr.strip()
            if remote_addr.startswith(PRIVATE_IPS_PREFIX) and is_valid_ip(remote_addr):
                ip_address = remote_addr.strip()
    if not ip_address:
            ip_address = '127.0.0.1'
    return ip_address


def get_ip(request):
    if not BEHIND_REVERSE_PROXY:
        ip = get_ip_address_from_request(request)
    else:
        ip = request.META.get(REVERSE_PROXY_HEADER, '')
        ip = ip.split(",", 1)[0].strip()
        if ip == '':
            if not BEHIND_REVERSE_PROXY_WITH_DIRECT_ACCESS:
                raise Warning('Axes is configured for operation behind a reverse proxy but could not find '\
                    'an HTTP header value {0}. Check your proxy server settings '\
                    'to make sure this header value is being passed.'.format(REVERSE_PROXY_HEADER))
            else:
                ip = request.META.get('REMOTE_ADDR', '')
                if ip not in IP_WHITELIST:
                    raise Warning('Axes is configured for operation behind a reverse proxy and to allow some'\
                        'IP addresses to have direct access. {0} is not on the white list'.format(ip))
    return ip


def get_lockout_url():
    return getattr(settings, 'AXES_LOCKOUT_URL', None)


def query2str(items):
    """Turns a dictionary into an easy-to-read list of key-value pairs.

    If there's a field called "password" it will be excluded from the output.
    """
    # Limit the length of the value to avoid a DoS attack
    value_maxlimit = 256

    kvs = []
    for k, v in items:
        if k != 'password':
            kvs.append(six.u('%s=%s') % (k, v[:256]))

    return '\n'.join(kvs)


def ip_in_whitelist(ip):
    if IP_WHITELIST is not None:
        return ip in IP_WHITELIST

    return False


def ip_in_blacklist(ip):
    if IP_BLACKLIST is not None:
        return ip in IP_BLACKLIST

    return False


def is_user_lockable(request):
    """Check if the user has a profile with nolockout
    If so, then return the value to see if this user is special
    and doesn't get their account locked out
    """
    try:
        field = getattr(User, 'USERNAME_FIELD', 'username')
        kwargs = {
            field: request.POST.get('username')
        }
        user = User.objects.get(**kwargs)
    except User.DoesNotExist:
        # not a valid user
        return True

    # Django 1.5 does not support profile anymore, ask directly to user
    if hasattr(user, 'nolockout'):
        # need to revert since we need to return
        # false for users that can't be blocked
        return not user.nolockout

    try:
        profile = user.get_profile()
    except (SiteProfileNotAvailable, ObjectDoesNotExist, AttributeError):
        # no profile
        return True

    if hasattr(profile, 'nolockout'):
        # need to revert since we need to return
        # false for users that can't be blocked
        return not profile.nolockout
    else:
        return True

def _get_user_attempts(request):
    """Returns access attempt record if it exists.
    Otherwise return None.
    """
    ip = get_ip(request)

    username = request.POST.get('username', None)

    if USE_USER_AGENT:
        ua = request.META.get('HTTP_USER_AGENT', '<unknown>')[:255]
        attempts = AccessAttempt.objects.filter(
            user_agent=ua, ip_address=ip, username=username, trusted=True
        )
    else:
        attempts = AccessAttempt.objects.filter(
            ip_address=ip, username=username, trusted=True
        )

    if not attempts:
        params = {'ip_address': ip, 'trusted': False}
        if USE_USER_AGENT:
            params['user_agent'] = ua

        attempts = AccessAttempt.objects.filter(**params)
        if username and not ip_in_whitelist(ip):
            del params['ip_address']
            params['username'] = username
            attempts |= AccessAttempt.objects.filter(**params)

    return attempts

def get_user_attempts(request):
    objects_deleted = False
    attempts = _get_user_attempts(request)

    if COOLOFF_TIME:
        for attempt in attempts:
            if attempt.attempt_time + COOLOFF_TIME < datetime.now():
                if attempt.trusted:
                    attempt.failures_since_start = 0
                    attempt.save()
                else:
                    attempt.delete()
                    objects_deleted = True

    # If objects were deleted, we need to update the queryset to reflect this,
    # so force a reload.
    if objects_deleted:
        attempts = _get_user_attempts(request)

    return attempts


def watch_login(func):
    """
    Used to decorate the django.contrib.admin.site.login method.
    """

    def decorated_login(request, *args, **kwargs):
        # share some useful information
        if func.__name__ != 'decorated_login' and VERBOSE:
            log.info('AXES: Calling decorated function: %s' % func.__name__)
            if args:
                log.info('args: %s' % str(args))
            if kwargs:
                log.info('kwargs: %s' % kwargs)

        # TODO: create a class to hold the attempts records and perform checks
        # with its methods? or just store attempts=get_user_attempts here and
        # pass it to the functions
        # also no need to keep accessing these:
        # ip = request.META.get('REMOTE_ADDR', '')
        # ua = request.META.get('HTTP_USER_AGENT', '<unknown>')
        # username = request.POST.get('username', None)

        # if the request is currently under lockout, do not proceed to the
        # login function, go directly to lockout url, do not pass go, do not
        # collect messages about this login attempt
        if is_already_locked(request):
            return lockout_response(request)

        # call the login function
        response = func(request, *args, **kwargs)

        if func.__name__ == 'decorated_login':
            # if we're dealing with this function itself, don't bother checking
            # for invalid login attempts.  I suppose there's a bunch of
            # recursion going on here that used to cause one failed login
            # attempt to generate 10+ failed access attempt records (with 3
            # failed attempts each supposedly)
            return response

        if request.method == 'POST':
            # see if the login was successful
            login_unsuccessful = (
                response and
                not response.has_header('location') and
                response.status_code != 302
            )

            access_log = AccessLog.objects.create(
                user_agent=request.META.get('HTTP_USER_AGENT', '<unknown>')[:255],
                ip_address=get_ip(request),
                username=request.POST.get('username', None),
                http_accept=request.META.get('HTTP_ACCEPT', '<unknown>'),
                path_info=request.META.get('PATH_INFO', '<unknown>'),
                trusted=not login_unsuccessful,
            )
            if check_request(request, login_unsuccessful):
                return response

            return lockout_response(request)

        return response

    return decorated_login


def lockout_response(request):
    if LOCKOUT_TEMPLATE:
        context = {
            'cooloff_time': COOLOFF_TIME,
            'failure_limit': FAILURE_LIMIT,
        }
        return render_to_response(LOCKOUT_TEMPLATE, context,
                                  context_instance=RequestContext(request))

    LOCKOUT_URL = get_lockout_url()
    if LOCKOUT_URL:
        return HttpResponseRedirect(LOCKOUT_URL)

    if COOLOFF_TIME:
        return HttpResponse("Account locked: too many login attempts.  "
                            "Please try again later.")
    else:
        return HttpResponse("Account locked: too many login attempts.  "
                            "Contact an admin to unlock your account.")


def is_already_locked(request):
    ip = get_ip(request)

    if ONLY_WHITELIST:
        if not ip_in_whitelist(ip):
            return True

    if ip_in_blacklist(ip):
        return True

    attempts = get_user_attempts(request)
    user_lockable = is_user_lockable(request)
    for attempt in attempts:
        if attempt.failures_since_start >= FAILURE_LIMIT and LOCK_OUT_AT_FAILURE and user_lockable:
            return True

    return False


def check_request(request, login_unsuccessful):
    ip_address = get_ip(request)
    username = request.POST.get('username', None)
    failures = 0
    attempts = get_user_attempts(request)

    for attempt in attempts:
        failures = max(failures, attempt.failures_since_start)

    if login_unsuccessful:
        # add a failed attempt for this user
        failures += 1

        # Create an AccessAttempt record if the login wasn't successful
        # has already attempted, update the info
        if len(attempts):
            for attempt in attempts:
                attempt.get_data = '%s\n---------\n%s' % (
                    attempt.get_data,
                    query2str(request.GET.items()),
                )
                attempt.post_data = '%s\n---------\n%s' % (
                    attempt.post_data,
                    query2str(request.POST.items())
                )
                attempt.http_accept = request.META.get('HTTP_ACCEPT', '<unknown>')
                attempt.path_info = request.META.get('PATH_INFO', '<unknown>')
                attempt.failures_since_start = failures
                attempt.attempt_time = datetime.now()
                attempt.save()
                log.info('AXES: Repeated login failure by %s. Updating access '
                         'record. Count = %s' %
                         (attempt.ip_address, failures))
        else:
            create_new_failure_records(request, failures)
    else:
        # user logged in -- forget the failed attempts
        failures = 0
        trusted_record_exists = False
        for attempt in attempts:
            if not attempt.trusted:
                attempt.delete()
            else:
                trusted_record_exists = True
                attempt.failures_since_start = 0
                attempt.save()

        if trusted_record_exists is False:
            create_new_trusted_record(request)

    user_lockable = is_user_lockable(request)
    # no matter what, we want to lock them out if they're past the number of
    # attempts allowed, unless the user is set to notlockable
    if failures > FAILURE_LIMIT and LOCK_OUT_AT_FAILURE and user_lockable:
        # We log them out in case they actually managed to enter the correct
        # password
        logout(request)
        log.warn('AXES: locked out %s after repeated login attempts.' %
                 (ip_address,))
        # send signal when someone is locked out.
        user_locked_out.send("axes", request=request, username=username, ip_address=ip_address)

        # if a trusted login has violated lockout, revoke trust
        for attempt in [a for a in attempts if a.trusted]:
            attempt.delete()
            create_new_failure_records(request, failures)

        return False

    return True


def create_new_failure_records(request, failures):
    ip = get_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', '<unknown>')[:255]
    username = request.POST.get('username', None)

    params = {
        'user_agent': ua,
        'ip_address': ip,
        'username': None,
        'get_data': query2str(request.GET.items()),
        'post_data': query2str(request.POST.items()),
        'http_accept': request.META.get('HTTP_ACCEPT', '<unknown>'),
        'path_info': request.META.get('PATH_INFO', '<unknown>'),
        'failures_since_start': failures,
    }

    # record failed attempt from this IP
    AccessAttempt.objects.create(**params)

    # record failed attempt on this username from untrusted IP
    params.update({
        'ip_address': None,
        'username': username,
    })
    AccessAttempt.objects.create(**params)

    log.info('AXES: New login failure by %s. Creating access record.' % (ip,))


def create_new_trusted_record(request):
    ip = get_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', '<unknown>')[:255]
    username = request.POST.get('username', None)

    if not username:
        return False

    AccessAttempt.objects.create(
        user_agent=ua,
        ip_address=ip,
        username=username,
        get_data=query2str(request.GET.items()),
        post_data=query2str(request.POST.items()),
        http_accept=request.META.get('HTTP_ACCEPT', '<unknown>'),
        path_info=request.META.get('PATH_INFO', '<unknown>'),
        failures_since_start=0,
        trusted=True
    )
