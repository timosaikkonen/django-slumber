"""
    Implements the user agent used to communicate with the Slumber
    servers.
"""
from django.conf import settings
from django.core.cache import cache
from django.test.client import Client as FakeClient, encode_multipart, \
    BOUNDARY
from django.utils.http import urlencode

from datetime import datetime
from fost_authn.signature import fost_hmac_request_signature
from httplib2 import Http
import logging
from ujson import dumps, loads
from urllib import urlencode
from urlparse import parse_qs, urlparse

from slumber._caches import PER_THREAD
from slumber.server import get_slumber_local_url_prefix


def _real():
    """Don't check certificates when we use httplib2.
    """
    return Http(disable_ssl_certificate_validation=True)


def _parse_qs(url):
    """Split the query string off (this is needed to support Django 1.0's
    fake HTTP client.
    """
    if url.find('?') >= 0:
        path, query_string = url.split('?')
        return path, parse_qs(query_string)
    else:
        return url, {}


def _use_fake(url):
    """Return the local URL fragment if the request should use the Fake
    HTTP client as it is local, otherwise return None
    """
    slumber_local = get_slumber_local_url_prefix()
    if url.startswith(slumber_local):
        logging.debug("Using local fake HTTP due to %s starting with %s",
            url, slumber_local)
        return url[len(slumber_local) - 1:]
    elif url.startswith('/'):
        logging.debug("Using local fake HTTP due to %s starting with /",
            url)
        return url
    logging.debug("Using real HTTP for %s", url)


def _calculate_signature(authn_name, method, url, body, username):
    """Do the signed request calculation.
    """
    # We need all arguments and all locals
    # pylint: disable=R0913
    # pylint: disable=R0914
    to_sign = {}
    if username:
        to_sign['X-FOST-User'] = username.encode('utf-7')
    if not isinstance(body, basestring):
        if method in ['POST', 'PUT']:
            logging.info("Encoding POST/PUT data %s", body or {})
            data = encode_multipart(BOUNDARY, body or {})
        else:
            logging.info("Encoding query string %s", body or {})
            data = urlencode(body or {}, doseq=True)
    else:
        data = body or ''
    now = datetime.utcnow().isoformat() + 'Z'
    _, signature = fost_hmac_request_signature(
        settings.SECRET_KEY, method, url, now, to_sign, data)
    headers = {}
    headers['Authorization'] = 'FOST %s:%s' % \
        (authn_name.encode('utf-7'), signature)
    headers['X-FOST-Timestamp'] = now
    headers['X-FOST-Headers'] = ' '.join(['X-FOST-Headers'] + to_sign.keys())
    for key, value in to_sign.items():
        headers[key] = value
    logging.debug("_calculate_signature %s adding headers: %s",
        method, headers)
    return headers


def for_user(name):
    """Decorator constructor that sets the user name to be used for requests.
    """
    def decorator(function):
        """The decorator.
        """
        def wrapped(*a, **kw):
            """The final wrapped function call.
            """
            old = getattr(PER_THREAD, 'username', None)
            try:
                PER_THREAD.username = name
                return function(*a, **kw)
            finally:
                PER_THREAD.username = old
        wrapped.__name__ = function.__name__
        return wrapped
    return decorator


def _sign_request(method, url, body):
    """Calculate the request headers that need to be added so that the
    request is properly signed and the Slumber server will consider
    the current user to be authenticated.
    """
    # import here avoids circular import
    from slumber.connector import get_slumber_authn_name
    authn_name = get_slumber_authn_name()
    if authn_name:
        name = getattr(PER_THREAD, 'username', None)
        return _calculate_signature(
            authn_name, method, url, body, name)
    else:
        return {}


def _fake_http_headers(headers):
    """Convert the headers into a form suitable for the Fake HTTP client.
    """
    return dict([('HTTP_' + k.upper().replace('-', '_'), v)
            for k, v in headers.items()])


def get(url, ttl=0, codes=None, headers=None):
    """Perform a GET request against a Slumber server.
    """
    return _get(url, ttl, codes, headers)


def _get(url, ttl, codes, headers):
    """Mockable version of the user agent get.
    """
    # Pylint gets confused by the fake HTTP client
    # pylint: disable=E1103
    codes = codes or [200]
    headers = headers or dict(Accept='application/json')
    url_fragment = _use_fake(url)
    if url_fragment:
        file_spec, query = _parse_qs(url_fragment)
        headers.update(_sign_request('GET', file_spec, query))
        response = FakeClient().get(file_spec, query,
            HTTP_HOST='localhost:8000', **_fake_http_headers(headers))
        if response.status_code in [301, 302] and \
                response.status_code not in codes:
            return get(response['location'], ttl, codes)
        assert response.status_code in codes, \
            (url_fragment, response, response.content)
        content = response.content
    else:
        cache_key = 'slumber.connector.ua.get.%s' % url
        cached = cache.get(cache_key)
        if not cached:
            logging.debug("Cache miss for url %s with cache key %s",
                url, cache_key)
            _, _, path, _, query, _ = urlparse(url)
            for _ in range(0, 3):
                headers.update(_sign_request('GET', path, query or ''))
                response, content = _real().request(
                    url, headers=headers)
                if response.status in codes:
                    break
            assert response.status in codes, \
                (url, response, content)
            if ttl:
                cache.set(cache_key, (response, content), ttl)
        else:
            logging.debug("Fetched %s from cache key %s", url, cache_key)
            response, content = cached
            response.from_cache = True
    try:
        return response, loads(content)
    except Exception:
        return response, {}


def delete(url, codes=None, headers=None):
    """Perform a DELETE request against a Slumber server.
    """
    return _delete(url, codes, headers)


def _delete(url, codes, headers):
    """Mockable version of the user agent DELETE.
    """
    # Pylint gets confused by the fake HTTP client
    # pylint: disable=E1103
    codes = codes or [200, 404, 410]
    headers = headers or dict(Accept='application/json')
    url_fragment = _use_fake(url)
    if url_fragment:
        headers.update(_sign_request('DELETE', url_fragment, ''))
        response = FakeClient().get(url_fragment, {},
            HTTP_HOST='localhost:8000',
            REQUEST_METHOD='DELETE', # Django 1.0 compatible
            **_fake_http_headers(headers))
        assert response.status_code in codes, \
            (url_fragment, response, response.content)
        content = response.content
    else:
        headers.update(_sign_request('DELETE', urlparse(url).path, ''))
        headers['Content-Type'] = 'application/json'
        response, content = _real().request(url, "DELETE", headers=headers)
        assert response.status in codes, (url, response, content)
    try:
        return response, loads(content)
    except Exception:
        return response, {}



def put(url, data, codes=None, headers=None):
    """Perform a PUT request against a Slumber server
    """
    return _put(url, data, codes, headers)


def _put(url, data, codes, headers):
    """Mockable version of the user agent put.
    """
    # pylint: disable=maybe-no-member
    codes = codes or [200, 201, 204]
    headers = headers or dict(Accept='application/json')
    body = dumps(data)
    url_fragment = _use_fake(url)
    if url_fragment:
        headers.update(_sign_request('PUT', url_fragment, body))
        response = FakeClient().post(url_fragment, body,
            content_type='application/json',
            HTTP_HOST='localhost:8000',
            REQUEST_METHOD='PUT', # Django 1.0 compatible
            **_fake_http_headers(headers))
        assert response.status_code in codes, \
            (url_fragment, response, response.content)
        content = response.content
    else:
        headers.update(_sign_request('PUT', urlparse(url).path, body))
        headers['Content-Type'] = 'application/json'
        response, content = _real().request(url, "PUT", body=body,
            headers=headers)
        assert response.status in codes, (url, response, content)
    try:
        return response, loads(content)
    except Exception:
        return response, {}


def post(url, data, codes=None):
    """Perform a POST request against a Slumber server.
    """
    return _post(url, data, codes)


def _post(url, data, codes):
    """Mockable version of the user agent post.
    """
    # Pylint gets confused by the urlparse return type
    # pylint: disable=E1101
    # Pylint gets confused by the fake HTTP client
    # pylint: disable=E1103
    headers = dict(Accept='application/json')
    body = dumps(data) if data else ''
    url_fragment = _use_fake(url)
    if url_fragment:
        headers.update(_sign_request('POST', url_fragment, body))
        response = FakeClient().post(url_fragment, body,
            content_type='application/json',
            HTTP_HOST='localhost:8000',
            **_fake_http_headers(headers))
        assert response.status_code in (codes or [200]), \
            (url_fragment, response, response.content)
        content = response.content
    else:
        headers.update(_sign_request('POST', urlparse(url).path, body))
        headers['Content-Type'] = 'application/json'
        response, content = _real().request(url, "POST", body=body,
            headers=headers)
        assert response.status in (codes or [200]), \
            (url, response, content)
    try:
        return response, loads(content)
    except Exception:
        return response, {}

