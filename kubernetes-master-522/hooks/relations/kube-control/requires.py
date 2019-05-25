#!/usr/bin/python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json

from charms.reactive import RelationBase
from charms.reactive import hook
from charms.reactive import scopes

from charmhelpers.core import hookenv


class KubeControlRequireer(RelationBase):
    """Implements the kubernetes-worker side of the kube-control interface.

    """
    scope = scopes.GLOBAL

    @hook('{requires:kube-control}-relation-{joined,changed}')
    def joined_or_changed(self):
        """Set states corresponding to the data we have.

        """
        conv = self.conversation()
        conv.set_state('{relation_name}.connected')

        if self.dns_ready():
            conv.set_state('{relation_name}.dns.available')
        else:
            conv.remove_state('{relation_name}.dns.available')

        if self._has_auth_credentials():
            conv.set_state('{relation_name}.auth.available')
        else:
            conv.remove_state('{relation_name}.auth.available')

        if self.get_cluster_tag():
            conv.set_state('{relation_name}.cluster_tag.available')
        else:
            conv.remove_state('{relation_name}.cluster_tag.available')

        if self.get_registry_location():
            conv.set_state('{relation_name}.registry_location.available')
        else:
            conv.remove_state('{relation_name}.registry_location.available')

    @hook('{requires:kube-control}-relation-{broken,departed}')
    def departed(self):
        """Remove all states.

        """
        conv = self.conversation()
        if len(conv.units) == 1:
            conv.remove_state('{relation_name}.connected')
            conv.remove_state('{relation_name}.dns.available')

    def get_auth_credentials(self, user):
        """ Return the authentication credentials.

        """
        conv = self.conversation()
        remote_creds = conv.get_remote('creds')
        if not remote_creds:
            return None

        all_creds = json.loads(remote_creds)
        if user in all_creds:
            return {
                'user': user,
                'kubelet_token': all_creds[user]['kubelet_token'],
                'proxy_token': all_creds[user]['proxy_token'],
                'client_token': all_creds[user]['client_token']
            }
        else:
            return None

    def get_dns(self):
        """Return DNS info provided by the master.

        """
        conv = self.conversation()

        return {
            'port': conv.get_remote('port'),
            'domain': conv.get_remote('domain'),
            'sdn-ip': conv.get_remote('sdn-ip'),
            'enable-kube-dns': conv.get_remote('enable-kube-dns'),
        }

    def dns_ready(self):
        """Return True if we have all DNS info from the master."""
        keys = ['port', 'domain', 'sdn-ip', 'enable-kube-dns']
        dns_info = self.get_dns()
        return (set(dns_info.keys()) == set(keys) and
                dns_info['enable-kube-dns'] is not None)

    def set_auth_request(self, kubelet, group='system:nodes'):
        """ Tell the master that we are requesting auth, and to use this
        hostname for the kubelet system account.

        Param groups - Determines the level of eleveted privleges of the
        requested user. Can be overridden to request sudo level access on the
        cluster via changing to system:masters """
        conv = self.conversation()
        conv.set_remote(data={'kubelet_user': kubelet,
                              'auth_group': group})

    def set_gpu(self, enabled=True):
        """Tell the master that we're gpu-enabled (or not).

        """
        hookenv.log('Setting gpu={} on kube-control relation'.format(enabled))
        conv = self.conversation()
        conv.set_remote(gpu=enabled)

    def _has_auth_credentials(self):
        """Predicate method to signal we have authentication credentials """
        conv = self.conversation()
        if conv.get_remote('creds'):
            return True

    def get_cluster_tag(self):
        """Tag for identifying resources that are part of the cluster."""
        return self.conversation().get_remote('cluster-tag')

    def get_registry_location(self):
        """URL for container image registry"""
        return self.conversation().get_remote('registry-location')
