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

from charms.reactive import RelationBase
from charms.reactive import hook
from charms.reactive import scopes


class KubeDNSProvider(RelationBase):
    scope = scopes.GLOBAL

    @hook('{provides:kube-dns}-relation-{joined,changed}')
    def joined_or_changed(self):
        conv = self.conversation()
        conv.set_state('{relation_name}.connected')

    @hook('{provides:kube-dns}-relation-{departed}')
    def departed(self):
        conv = self.conversation()
        conv.remove_state('{relation_name}.connected')

    def set_dns_info(self, port, domain, sdn_ip):
        ''' We will need the domain, sdn_ip, and port of the dns service, if
            sdn_ip is not required in your deployment, the units private-ip
            is availble implicitly.'''
        credentials = {'port': port,
                       'domain': domain,
                       'sdn-ip': sdn_ip}
        conv = self.conversation()
        conv.set_remote(data=credentials)
