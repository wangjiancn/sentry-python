import sys
import weakref
from inspect import isawaitable

from sentry_sdk import Hub, push_scope, configure_scope
from sentry_sdk._compat import urlparse, reraise
from sentry_sdk.utils import capture_internal_exceptions, event_from_exception
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations._wsgi import RequestExtractor, _filter_headers

from sanic import Sanic
from sanic.router import Router
from sanic.handlers import ErrorHandler


class SanicIntegration(Integration):
    identifier = "sanic"

    def install(self):
        if sys.version_info < (3, 7):
            # Sanic is async. We better have contextvars or we're going to leak
            # state between requests.
            raise RuntimeError("The sanic integration for Sentry requires Python 3.7+")

        old_handle_request = Sanic.handle_request

        async def sentry_handle_request(self, request, *args, **kwargs):
            weak_request = weakref.ref(request)

            with push_scope() as scope:
                scope.add_event_processor(_make_request_processor(weak_request))
                response = old_handle_request(self, request, *args, **kwargs)
                if isawaitable(response):
                    response = await response
                return response

        Sanic.handle_request = sentry_handle_request

        old_router_get = Router.get

        def sentry_router_get(self, request):
            rv = old_router_get(self, request)
            with capture_internal_exceptions():
                with configure_scope() as scope:
                    scope.transaction = rv[0].__name__
            return rv

        Router.get = sentry_router_get

        old_error_handler_lookup = ErrorHandler.lookup

        def sentry_error_handler_lookup(self, exception):
            _capture_exception(exception)
            old_error_handler = old_error_handler_lookup(self, exception)

            if old_error_handler is None:
                return None

            async def sentry_wrapped_error_handler(request, exception):
                try:
                    response = old_error_handler(request, exception)
                    if isawaitable(response):
                        response = await response
                    return response
                except Exception:
                    exc_info = sys.exc_info()
                    _capture_exception(exc_info)
                    reraise(*exc_info)

            return sentry_wrapped_error_handler

        ErrorHandler.lookup = sentry_error_handler_lookup


def _capture_exception(exception):
    with capture_internal_exceptions():
        hub = Hub.current
        event, hint = event_from_exception(
            exception,
            with_locals=hub.client.options["with_locals"],
            mechanism={"type": "sanic", "handled": False},
        )

        hub.capture_event(event, hint=hint)


def _make_request_processor(weak_request):
    def sanic_processor(event, hint):
        request = weak_request()
        if request is None:
            return event

        with capture_internal_exceptions():
            extractor = SanicRequestExtractor(request)
            extractor.extract_into_event(event)

            request_info = event["request"]
            if "query_string" not in request_info:
                request_info["query_string"] = extractor.urlparts.query

            if "method" not in request_info:
                request_info["method"] = request.method

            if "env" not in request_info:
                request_info["env"] = {"REMOTE_ADDR": request.remote_addr}

            if "headers" not in request_info:
                request_info["headers"] = _filter_headers(dict(request.headers))

        return event

    return sanic_processor


class SanicRequestExtractor(RequestExtractor):
    def __init__(self, request):
        RequestExtractor.__init__(self, request)
        self.urlparts = urlparse.urlsplit(self.request.url)

    def content_length(self):
        if self.request.body is None:
            return 0
        return len(self.request.body)

    def url(self):
        return "%s://%s%s" % (
            self.urlparts.scheme,
            self.urlparts.netloc,
            self.urlparts.path,
        )

    def cookies(self):
        return dict(self.request.cookies)

    def raw_data(self):
        return self.request.body

    def form(self):
        return self.request.form

    def is_json(self):
        raise NotImplementedError()

    def json(self):
        return self.request.json

    def files(self):
        return self.request.files

    def size_of_file(self, file):
        return len(file.body or ())