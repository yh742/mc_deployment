#!/usr/bin/python

from charms.reactive import Endpoint
from charms.reactive import when, when_not
from charms.reactive import set_state, remove_state


class CNIPluginClient(Endpoint):

    @when('endpoint.{endpoint_name}.changed')
    def changed(self):
        ''' Indicate the relation is connected, and if the relation data is
        set it is also available. '''
        set_state(self.expand_name('{endpoint_name}.connected'))
        config = self.get_config()
        if config['is_master'] == 'True':
            set_state(self.expand_name('{endpoint_name}.is-master'))
            set_state(self.expand_name('{endpoint_name}.configured'))
        elif config['is_master'] == 'False':
            set_state(self.expand_name('{endpoint_name}.is-worker'))
            set_state(self.expand_name('{endpoint_name}.configured'))
        else:
            remove_state(self.expand_name('{endpoint_name}.configured'))
        remove_state(self.expand_name('endpoint.{endpoint_name}.changed'))

    @when_not('endpoint.{endpoint_name}.joined')
    def broken(self):
        ''' Indicate the relation is no longer available and not connected. '''
        remove_state(self.expand_name('{endpoint_name}.connected'))
        remove_state(self.expand_name('{endpoint_name}.is-master'))
        remove_state(self.expand_name('{endpoint_name}.is-worker'))
        remove_state(self.expand_name('{endpoint_name}.configured'))

    def get_config(self):
        ''' Get the kubernetes configuration information. '''
        return self.all_joined_units.received_raw

    def set_config(self, cidr):
        ''' Sets the CNI configuration information. '''
        for relation in self.relations:
            relation.to_publish_raw.update({
                'cidr': cidr,
            })
