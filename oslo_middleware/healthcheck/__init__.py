# Copyright 2011 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json
import socket

try:
    from collections import OrderedDict  # noqa
except ImportError:
    # TODO(harlowja): remove this when py2.6 support is dropped...
    from ordereddict import OrderedDict  # noqa

import jinja2
from oslo_utils import reflection
from oslo_utils import strutils
import six
import stevedore
import webob.dec
import webob.exc
import webob.response

from oslo_middleware import base


def _expand_template(contents, params):
    tpl = jinja2.Template(source=contents,
                          undefined=jinja2.StrictUndefined)
    return tpl.render(**params)


class Healthcheck(base.ConfigurableMiddleware):
    """Healthcheck middleware used for monitoring.

    If the path is /healthcheck, it will respond 200 with "OK" as the body.
    Or 503 with the reason as the body if one of the backend report
    an application issue.

    This is useful for the following reasons:

    1. Load balancers can 'ping' this url to determine service availability.
    2. Provides an endpoint that is similar to 'mod_status' in apache which
       can provide details (or no details, depending on if configured) about
       the activity of the server.

    Example requests/responses:

        $ curl -i -X HEAD "http://0.0.0.0:8775/status"
        HTTP/1.1 204 No Content
        Content-Type: text/plain; charset=UTF-8
        Content-Length: 0
        Date: Fri, 11 Sep 2015 18:55:08 GMT

        $ curl -i  "http://0.0.0.0:8775/status"
        HTTP/1.1 200 OK
        Content-Type: text/plain; charset=UTF-8
        Content-Length: 2
        Date: Fri, 11 Sep 2015 18:55:43 GMT

        OK

    Example of paste configuration:

    .. code-block:: ini

        [filter:healthcheck]
        paste.filter_factory = oslo_middleware:Healthcheck.factory
        path = /healthcheck
        backends = disable_by_file
        disable_by_file_path = /var/run/nova/healthcheck_disable

        [pipeline:public_api]
        pipeline = healthcheck sizelimit [...] public_service


    Multiple filter sections can be defined if it desired to have
    pipelines with different healthcheck configuration, example:

    .. code-block:: ini

        [pipeline:public_api]
        pipeline = healthcheck_public sizelimit [...] public_service

        [pipeline:admin_api]
        pipeline = healthcheck_admin sizelimit [...] admin_service

        [filter:healthcheck_public]
        paste.filter_factory = oslo_middleware:Healthcheck.factory
        path = /healthcheck_public
        backends = disable_by_file
        disable_by_file_path = /var/run/nova/healthcheck_public_disable

        [filter:healthcheck_admin]
        paste.filter_factory = oslo_middleware:Healthcheck.factory
        path = /healthcheck_admin
        backends = disable_by_file
        disable_by_file_path = /var/run/nova/healthcheck_admin_disable

    More details on available backends and their configuration can be found
    on this page: :doc:`healthcheck_plugins`.

    """

    NAMESPACE = "oslo.middleware.healthcheck"
    HEALTHY_TO_STATUS_CODES = {
        True: webob.exc.HTTPOk.code,
        False: webob.exc.HTTPServiceUnavailable.code,
    }
    HEAD_HEALTHY_TO_STATUS_CODES = {
        True: webob.exc.HTTPNoContent.code,
        False: webob.exc.HTTPServiceUnavailable.code,
    }
    PLAIN_RESPONSE_TEMPLATE = """
{% for reason in reasons %}
{% if reason %}{{reason}}{% endif -%}
{% endfor %}
"""

    HTML_RESPONSE_TEMPLATE = """
<HTML>
<HEAD><TITLE>Healthcheck Status</TITLE></HEAD>
<BODY>
{% if detailed -%}
{% if hostname -%}
<H1>Server status for {{hostname|e}}</H1>
{%- endif %}
{%- endif %}
<H2>Result of {{results|length}} checks:</H2>
<TABLE bgcolor="#ffffff" border="1">
<TBODY>
{% for result in results -%}
{% if result.reason -%}
<TR>
{% if detailed -%}
    <TD>{{result.class|e}}</TD>
{%- endif %}
    <TD>{{result.reason|e}}</TD>
</TR>
{%- endif %}
{%- endfor %}
</TBODY>
</TABLE>
</BODY>
</HTML>
"""

    def __init__(self, application, conf):
        super(Healthcheck, self).__init__(application)
        self._path = conf.get('path', '/healthcheck')
        self._show_details = strutils.bool_from_string(conf.get('detailed'))
        self._backend_names = []
        backends = conf.get('backends')
        if backends:
            self._backend_names = backends.split(',')
        self._backends = stevedore.NamedExtensionManager(
            self.NAMESPACE, self._backend_names,
            name_order=True, invoke_on_load=True,
            invoke_args=(conf,))
        self._accept_to_functor = OrderedDict([
            # Order here matters...
            ('text/plain', self._make_text_response),
            ('text/html', self._make_html_response),
            ('application/json', self._make_json_response),
        ])
        self._accept_order = tuple(six.iterkeys(self._accept_to_functor))
        # When no accept type matches instead of returning 406 we will
        # always return text/plain (because sending an error from this
        # middleware actually can cause issues).
        self._default_accept = 'text/plain'

    @staticmethod
    def _pretty_json_dumps(contents):
        return json.dumps(contents, indent=4, sort_keys=True)

    @staticmethod
    def _are_results_healthy(results):
        for result in results:
            if not result.available:
                return False
        return True

    def _make_text_response(self, results, healthy):
        params = {
            'reasons': [result.reason for result in results],
            'detailed': self._show_details,
        }
        body = _expand_template(self.PLAIN_RESPONSE_TEMPLATE, params)
        return (body.strip(), 'text/plain')

    def _make_json_response(self, results, healthy):
        if self._show_details:
            body = {
                'detailed': True,
            }
            reasons = []
            for result in results:
                reasons.append({
                    'reason': result.reason,
                    'class': reflection.get_class_name(result,
                                                       fully_qualified=False),
                })
            body['reasons'] = reasons
        else:
            body = {
                'reasons': [result.reason for result in results],
                'detailed': False,
            }
        return (self._pretty_json_dumps(body), 'application/json')

    def _make_head_response(self, results, healthy):
        return ( "", "text/plain")

    def _make_html_response(self, results, healthy):
        try:
            hostname = socket.gethostname()
        except socket.error:
            hostname = None
        translated_results = []
        for result in results:
            translated_results.append({
                'reason': result.reason,
                'class': reflection.get_class_name(result,
                                                   fully_qualified=False),
            })
        params = {
            'healthy': healthy,
            'hostname': hostname,
            'results': translated_results,
            'detailed': self._show_details,
        }
        body = _expand_template(self.HTML_RESPONSE_TEMPLATE, params)
        return (body.strip(), 'text/html')

    @webob.dec.wsgify
    def process_request(self, req):
        if req.path != self._path:
            return None
        results = [ext.obj.healthcheck() for ext in self._backends]
        healthy = self._are_results_healthy(results)
        if req.method == "HEAD":
            functor = self._make_head_response
            status = self.HEAD_HEALTHY_TO_STATUS_CODES[healthy]
        else:
            status = self.HEALTHY_TO_STATUS_CODES[healthy]
            accept_type = req.accept.best_match(self._accept_order)
            if not accept_type:
                accept_type = self._default_accept
            functor = self._accept_to_functor[accept_type]
        body, content_type = functor(results, healthy)
        return webob.response.Response(status=status, body=body,
                                       content_type=content_type)
