#!/usr/bin/python

from charms.reactive import Endpoint
from charms.reactive import when, when_not
from charms.reactive import set_state, remove_state


class CNIPluginProvider(Endpoint):

    @when('endpoint.{endpoint_name}.changed')
    def changed(self):
        ''' Set the connected state from the provides side of the relation. '''
        set_state(self.expand_name('{endpoint_name}.connected'))
        if self.config_available():
            set_state(self.expand_name('{endpoint_name}.available'))
        remove_state(self.expand_name('endpoint.{endpoint_name}.changed'))

    @when_not('endpoint.{endpoint_name}.joined')
    def broken_or_departed(self):
        '''Remove connected state from the provides side of the relation. '''
        remove_state(self.expand_name('{endpoint_name}.connected'))
        remove_state(self.expand_name('{endpoint_name}.available'))
        remove_state(self.expand_name('{endpoint_name}.configured'))

    def set_config(self, is_master, kubeconfig_path):
        ''' Relays a dict of kubernetes configuration information. '''
        for relation in self.relations:
            relation.to_publish_raw.update({
                'is_master': is_master,
                'kubeconfig_path': kubeconfig_path
            })
        set_state(self.expand_name('{endpoint_name}.configured'))

    def config_available(self):
        ''' Ensures all config from the CNI plugin is available. '''
        cidr = self.all_joined_units.received_raw['cidr']
        if not cidr:
            return False
        return True

    def get_config(self):
        ''' Returns all config from the CNI plugin. '''
        return self.all_joined_units.received_raw
