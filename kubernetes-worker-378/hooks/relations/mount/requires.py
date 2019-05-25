from charms.reactive import when, when_not
from charms.reactive import set_flag, clear_flag
from charms.reactive import Endpoint


class MountRequires(Endpoint):

    @when('endpoint.{endpoint_name}.changed')
    def changed(self):
        if any(unit.received_raw['mountpoint']
               for unit in self.all_joined_units):
            set_flag(self.expand_name('{endpoint_name}.available'))

    @when_not('endpoint.{endpoint_name}.joined')
    def broken(self):
        clear_flag(self.expand_name('{endpoint_name}.available'))

    def mounts(self):
        """
        Returns a list of available mounts and their associated data.

        The return value is a list of dicts of the following form::

            [
                {
                    'mount_name': name_of_mount,
                    'mounts': [
                        {
                            'hostname': hostname,
                            'mountpoint': mountpoint,
                            'fstype': mounttype,
                            'options': options
                        },
                        # ...
                    ],
                },
                # ...
            ]
        """
        mounts = {}
        for relation in self.relations:
            mount_name = relation.application_name
            mount = mounts.setdefault(mount_name, {
                'mount_name': mount_name,
                'mounts': [],
            })
            data = relation.joined_units.received_raw
            mountpoint = data['mountpoint']
            fstype = data['fstype']
            options = data['options']
            host = data['hostname'] or \
                data['private-address']
            if host and mountpoint and fstype and options:
                mount['mounts'].append({
                    'hostname': host,
                    'mountpoint': mountpoint,
                    'fstype': fstype,
                    'options': options
                })
        return [m for m in mounts.values() if m['mounts']]
