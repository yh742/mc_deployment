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


class KubeDNSRequireer(RelationBase):
    scope = scopes.GLOBAL

    @hook('{requires:kube-dns}-relation-{joined,changed}')
    def joined_or_changed(self):
        ''' Set the available state if we have the minimum credentials '''
        if self.has_info():
            conv = self.conversation()
            conv.set_state('{relation_name}.available')

    def details(self):
        ''' Return a small subnet of the data '''
        return {'private-address': self._get_value('private-address'),
                'port': self._get_value('port'),
                'domain': self._get_value('domain'),
                'sdn-ip': self._get_value('sdn-ip')}

    def has_info(self):
        ''' Determine if we have a hostname and a port and domain '''
        to_find = ['private-address', 'port', 'domain', 'sdn-ip']
        # Iterate through our services and verify we have values
        for value in to_find:
            if not self._get_value(value):
                return False
        return True

    def _get_value(self, key):
        conv = self.conversation()
        return conv.get_remote(key)
