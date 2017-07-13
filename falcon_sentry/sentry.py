# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division

import logging

import falcon
from raven import Client, setup_logging
from raven.handlers.logging import SentryHandler
from raven.utils.encoding import to_unicode
from raven.utils.wsgi import get_environ


class Sentry(object):
    """
    Falcon application for Sentry.

    Look up configuration from ``os.environ['SENTRY_DSN']``::

    >>> sentry = Sentry()

    Pass an arbitrary DSN::

    >>> sentry = Sentry(dsn='http://public:secret@example.com/1')

    Pass an explicit client::

    >>> sentry = Sentry(client=client)

    Automatically configure logging::

    >>> sentry = Sentry(logging=True, level=logging.ERROR)

    Capture an exception::

    >>> try:
    >>>     1 / 0
    >>> except ZeroDivisionError:
    >>>     sentry.captureException()

    Capture a message::

    >>> sentry.captureMessage('hello, world!')

    """

    def __init__(self, client=None, client_cls=Client, dsn=None,
                 handle_logging=False, logging_exclusions=None,
                 level=logging.NOTSET,
                 user_context_loader=None,
                 body_context_loader=None,
                 **options):

        if client and not isinstance(client, Client):
            raise TypeError('client should be an instance of Client')

        self.dsn = dsn
        self.client = client if client else client_cls(dsn=dsn, **options)
        if handle_logging:
            self.configure_logging(level,
                                   logging_exclusions=logging_exclusions)
        self.body_context_loader = body_context_loader
        self.user_context_loader = user_context_loader

    @property
    def middleware(self):
        return SentryMiddleware(self,
                                self.body_context_loader,
                                self.user_context_loader)

    def configure_logging(self, level=None, **kwargs):
        exlcude = kwargs.pop('logging_exclusions', None)

        if exlcude:
            kwargs['exclude'] = exlcude
        setup_logging(SentryHandler(self.client, level=level), **kwargs)

    def get_error_handler(self, only_500=False):
        def error_handler(ex, req, resp, params):
            raise_exc = ex

            if isinstance(ex, falcon.HTTPStatus):
                raise raise_exc
            elif isinstance(ex, falcon.HTTPError):
                code = ex.code or 500

                if only_500 and code < 500:
                    raise raise_exc

                level = 'errors' if code < 500 else 'fatal'
                extra = ex.to_dict()
                extra.update(params)
            else:
                level = 'fatal'
                extra = dict(params)
                raise_exc = falcon.HTTPInternalServerError(title='Unknown Error',
                                                           description=str(ex))

            self.client.captureException(level=level, extra=extra)
            raise raise_exc

        return error_handler

    def user_context(self, data):
        self.client.user_context(data)

    def tags_context(self, tags, **kwargs):
        self.client.tags_context(tags, **kwargs)

    def extra_context(self, data, **kwargs):
        self.client.extra_context(data, **kwargs)

    def http_context(self, data, **kwargs):
        context = self.client.context.get()
        req_context = context.get('request', {})
        req_context.update(data)
        self.client.http_context(req_context, **kwargs)

    def clear_context(self):
        self.client.context.clear()
        self.last_event_id = None

    def captureException(self, *args, **kwargs):
        result = self.client.captureException(*args, **kwargs)
        self.last_event_id = result
        return result

    def captureMessage(self, *args, **kwargs):
        result = self.client.captureMessage(*args, **kwargs)
        self.last_event_id = result
        return result

    def _log_exception(self, ex):
        self.client.logger.exception(to_unicode(ex))


class SentryMiddleware(object):
    """
    A falcon middleware that setups request context for sentry for each incoming
    request.
    """
    def __init__(self, client,
                 user_context_loader=None,
                 body_context_loader=None):
        self.client = client
        self.body_context_loader = body_context_loader
        self.user_context_loader = user_context_loader

    def get_request_context(self, req):
        return {
            'url': '{}://{}{}'.format(req.protocol, req.host, req.path),
            'query_string': req.query_string,
            'params': req.params,
            'headers': req.headers,
            'route': req.uri_template,
            'method': req.method,
            'env': dict(get_environ(req.env))
        }

    def process_request(self, req, *args, **kwargs):
        try:
            req_context = self.get_request_context(req)
            if self.body_context_loader:
                req_context['body'] = self.body_context_loader(req)
            self.client.http_context(req_context)
        except Exception as ex:
            self.client._log_exception(ex)

        try:
            if self.user_context_loader:
                user_context = self.user_context_loader(req)
                self.client.user_context(user_context)
        except Exception as ex:
            self.client._log_exception(ex)

    def process_response(self, req, resp, *args, **kwargs):
        if self.client.last_event_id:
            resp.headers['X-Sentry-ID'] = self.client.last_event_id
        self.client.clear_context()

