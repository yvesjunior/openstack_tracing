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

from oslo_serialization import jsonutils as json

from tempest.common import service_client


class IdentityClient(service_client.ServiceClient):
    api_version = "v2.0"

    def show_api_description(self):
        """Retrieves info about the v2.0 Identity API"""
        url = ''
        resp, body = self.get(url)
        self.expected_success([200, 203], resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)

    def show_token(self, token_id):
        """Get token details."""
        resp, body = self.get("tokens/%s" % token_id)
        self.expected_success(200, resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)

    def delete_token(self, token_id):
        """Delete a token."""
        resp, body = self.delete("tokens/%s" % token_id)
        self.expected_success(204, resp.status)
        return service_client.ResponseBody(resp, body)

    def create_service(self, name, type, **kwargs):
        """Create a service."""
        post_body = {
            'name': name,
            'type': type,
            'description': kwargs.get('description')
        }
        post_body = json.dumps({'OS-KSADM:service': post_body})
        resp, body = self.post('/OS-KSADM/services', post_body)
        self.expected_success(200, resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)

    def show_service(self, service_id):
        """Get Service."""
        url = '/OS-KSADM/services/%s' % service_id
        resp, body = self.get(url)
        self.expected_success(200, resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)

    def list_services(self):
        """List Service - Returns Services."""
        resp, body = self.get('/OS-KSADM/services')
        self.expected_success(200, resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)

    def delete_service(self, service_id):
        """Delete Service."""
        url = '/OS-KSADM/services/%s' % service_id
        resp, body = self.delete(url)
        self.expected_success(204, resp.status)
        return service_client.ResponseBody(resp, body)

    def create_endpoint(self, service_id, region_id, **kwargs):
        """Create an endpoint for service."""
        post_body = {
            'service_id': service_id,
            'region': region_id,
            'publicurl': kwargs.get('publicurl'),
            'adminurl': kwargs.get('adminurl'),
            'internalurl': kwargs.get('internalurl')
        }
        post_body = json.dumps({'endpoint': post_body})
        resp, body = self.post('/endpoints', post_body)
        self.expected_success(200, resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)

    def list_endpoints(self):
        """List Endpoints - Returns Endpoints."""
        resp, body = self.get('/endpoints')
        self.expected_success(200, resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)

    def delete_endpoint(self, endpoint_id):
        """Delete an endpoint."""
        url = '/endpoints/%s' % endpoint_id
        resp, body = self.delete(url)
        self.expected_success(204, resp.status)
        return service_client.ResponseBody(resp, body)

    def list_extensions(self):
        """List all the extensions."""
        resp, body = self.get('/extensions')
        self.expected_success(200, resp.status)
        body = json.loads(body)
        return service_client.ResponseBody(resp, body)