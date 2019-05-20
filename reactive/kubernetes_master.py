#!/usr/bin/env python

# Copyright 2015 The Kubernetes Authors.
#
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

import base64
import os
import re
import random
import shutil
import socket
import string
import json
import ipaddress
import traceback

from charms.leadership import leader_get, leader_set

from shutil import move, copyfile
from pathlib import Path
from subprocess import check_call
from subprocess import check_output
from subprocess import CalledProcessError
from urllib.request import Request, urlopen

from charms import layer
from charms.layer import snap
from charms.reactive import hook
from charms.reactive import remove_state
from charms.reactive import set_state
from charms.reactive import is_state
from charms.reactive import endpoint_from_flag
from charms.reactive import when, when_any, when_not, when_none
from charms.reactive.helpers import data_changed, any_file_changed

from charms.layer import tls_client

from charmhelpers.core import hookenv
from charmhelpers.core import host
from charmhelpers.core import unitdata
from charmhelpers.core.host import service_stop
from charmhelpers.core.templating import render
from charmhelpers.fetch import apt_install
from charmhelpers.contrib.charmsupport import nrpe

from charms.layer.kubernetes_common import kubeclientconfig_path
from charms.layer.kubernetes_common import migrate_resource_checksums
from charms.layer.kubernetes_common import check_resources_for_upgrade_needed
from charms.layer.kubernetes_common import calculate_and_store_resource_checksums  # noqa
from charms.layer.kubernetes_common import arch
from charms.layer.kubernetes_common import service_restart
from charms.layer.kubernetes_common import get_ingress_address
from charms.layer.kubernetes_common import create_kubeconfig
from charms.layer.kubernetes_common import get_service_ip
from charms.layer.kubernetes_common import configure_kubernetes_service
from charms.layer.kubernetes_common import cloud_config_path
from charms.layer.kubernetes_common import write_gcp_snap_config
from charms.layer.kubernetes_common import write_openstack_snap_config
from charms.layer.kubernetes_common import write_azure_snap_config
from charms.layer.kubernetes_common import configure_kube_proxy
from charms.layer.kubernetes_common import kubeproxyconfig_path
from charms.layer.kubernetes_common import kubectl_manifest
from charms.layer.kubernetes_common import get_version
from charms.layer.kubernetes_common import retry

# Override the default nagios shortname regex to allow periods, which we
# need because our bin names contain them (e.g. 'snap.foo.daemon'). The
# default regex in charmhelpers doesn't allow periods, but nagios itself does.
nrpe.Check.shortname_re = r'[\.A-Za-z0-9-_]+$'

snap_resources = ['kubectl', 'kube-apiserver', 'kube-controller-manager',
                  'kube-scheduler', 'cdk-addons', 'kube-proxy']

master_services = ['kube-apiserver',
                   'kube-controller-manager',
                   'kube-scheduler',
                   'kube-proxy']


os.environ['PATH'] += os.pathsep + os.path.join(os.sep, 'snap', 'bin')
db = unitdata.kv()
checksum_prefix = 'kubernetes-master.resource-checksums.'
configure_prefix = 'kubernetes-master.prev_args.'
keystone_root = '/root/cdk/keystone'


def set_upgrade_needed(forced=False):
    set_state('kubernetes-master.upgrade-needed')
    config = hookenv.config()
    previous_channel = config.previous('channel')
    require_manual = config.get('require-manual-upgrade')
    hookenv.log('set upgrade needed')
    if previous_channel is None or not require_manual or forced:
        hookenv.log('forcing upgrade')
        set_state('kubernetes-master.upgrade-specified')


@when('config.changed.channel')
def channel_changed():
    set_upgrade_needed()


def service_cidr():
    ''' Return the charm's service-cidr config '''
    frozen_cidr = db.get('kubernetes-master.service-cidr')
    return frozen_cidr or hookenv.config('service-cidr')


def freeze_service_cidr():
    ''' Freeze the service CIDR. Once the apiserver has started, we can no
    longer safely change this value. '''
    db.set('kubernetes-master.service-cidr', service_cidr())


def maybe_install_kube_proxy():
    if not snap.is_installed('kube-proxy'):
        channel = hookenv.config('channel')
        hookenv.status_set('maintenance', 'Installing kube-proxy snap')
        snap.install('kube-proxy', channel=channel, classic=True)
        calculate_and_store_resource_checksums(checksum_prefix, snap_resources)


@hook('upgrade-charm')
def check_for_upgrade_needed():
    '''An upgrade charm event was triggered by Juju, react to that here.'''
    hookenv.status_set('maintenance', 'Checking resources')

    # migrate to new flags
    if is_state('kubernetes-master.restarted-for-cloud'):
        remove_state('kubernetes-master.restarted-for-cloud')
        set_state('kubernetes-master.cloud.ready')
    if is_state('kubernetes-master.cloud-request-sent'):
        # minor change, just for consistency
        remove_state('kubernetes-master.cloud-request-sent')
        set_state('kubernetes-master.cloud.request-sent')

    # ceph-storage.configured flag no longer exists
    remove_state('ceph-storage.configured')

    # reconfigure ceph. we need this in case we're reverting from ceph-csi back
    # to old ceph on Kubernetes 1.10 or 1.11
    remove_state('kubernetes-master.ceph.configured')

    migrate_from_pre_snaps()
    maybe_install_kube_proxy()
    add_rbac_roles()
    set_state('reconfigure.authentication.setup')
    remove_state('authentication.setup')

    if not db.get('snap.resources.fingerprint.initialised'):
        # We are here on an upgrade from non-rolling master
        # Since this upgrade might also include resource updates eg
        # juju upgrade-charm kubernetes-master --resource kube-any=my.snap
        # we take no risk and forcibly upgrade the snaps.
        # Forcibly means we do not prompt the user to call the upgrade action.
        set_upgrade_needed(forced=True)

    migrate_resource_checksums(checksum_prefix, snap_resources)
    if check_resources_for_upgrade_needed(checksum_prefix, snap_resources):
        set_upgrade_needed()

    # Set the auto storage backend to etcd2.
    auto_storage_backend = leader_get('auto_storage_backend')
    is_leader = is_state('leadership.is_leader')
    if not auto_storage_backend and is_leader:
        leader_set(auto_storage_backend='etcd2')


def add_rbac_roles():
    '''Update the known_tokens file with proper groups.'''

    tokens_fname = '/root/cdk/known_tokens.csv'
    tokens_backup_fname = '/root/cdk/known_tokens.csv.backup'
    move(tokens_fname, tokens_backup_fname)
    with open(tokens_fname, 'w') as ftokens:
        with open(tokens_backup_fname, 'r') as stream:
            for line in stream:
                record = line.strip().split(',')
                # token, username, user, groups
                if record[2] == 'admin' and len(record) == 3:
                    towrite = '{0},{1},{2},"{3}"\n'.format(record[0],
                                                           record[1],
                                                           record[2],
                                                           'system:masters')
                    ftokens.write(towrite)
                    continue
                if record[2] == 'kube_proxy':
                    towrite = '{0},{1},{2}\n'.format(record[0],
                                                     'system:kube-proxy',
                                                     'kube-proxy')
                    ftokens.write(towrite)
                    continue
                if record[2] == 'kubelet' and record[1] == 'kubelet':
                    continue

                ftokens.write('{}'.format(line))


def rename_file_idempotent(source, destination):
    if os.path.isfile(source):
        os.rename(source, destination)


def migrate_from_pre_snaps():
    # remove old states
    remove_state('kubernetes.components.installed')
    remove_state('kubernetes.dashboard.available')
    remove_state('kube-dns.available')
    remove_state('kubernetes-master.app_version.set')

    # disable old services
    pre_snap_services = ['kube-apiserver',
                         'kube-controller-manager',
                         'kube-scheduler']
    for service in pre_snap_services:
        service_stop(service)

    # rename auth files
    os.makedirs('/root/cdk', exist_ok=True)
    rename_file_idempotent('/etc/kubernetes/serviceaccount.key',
                           '/root/cdk/serviceaccount.key')
    rename_file_idempotent('/srv/kubernetes/basic_auth.csv',
                           '/root/cdk/basic_auth.csv')
    rename_file_idempotent('/srv/kubernetes/known_tokens.csv',
                           '/root/cdk/known_tokens.csv')

    # cleanup old files
    files = [
        "/lib/systemd/system/kube-apiserver.service",
        "/lib/systemd/system/kube-controller-manager.service",
        "/lib/systemd/system/kube-scheduler.service",
        "/etc/default/kube-defaults",
        "/etc/default/kube-apiserver.defaults",
        "/etc/default/kube-controller-manager.defaults",
        "/etc/default/kube-scheduler.defaults",
        "/srv/kubernetes",
        "/home/ubuntu/kubectl",
        "/usr/local/bin/kubectl",
        "/usr/local/bin/kube-apiserver",
        "/usr/local/bin/kube-controller-manager",
        "/usr/local/bin/kube-scheduler",
        "/etc/kubernetes"
    ]
    for file in files:
        if os.path.isdir(file):
            hookenv.log("Removing directory: " + file)
            shutil.rmtree(file)
        elif os.path.isfile(file):
            hookenv.log("Removing file: " + file)
            os.remove(file)


@when('kubernetes-master.upgrade-specified')
def do_upgrade():
    install_snaps()
    remove_state('kubernetes-master.upgrade-needed')
    remove_state('kubernetes-master.upgrade-specified')


def install_snaps():
    channel = hookenv.config('channel')
    hookenv.status_set('maintenance', 'Installing kubectl snap')
    snap.install('kubectl', channel=channel, classic=True)
    hookenv.status_set('maintenance', 'Installing kube-apiserver snap')
    snap.install('kube-apiserver', channel=channel)
    hookenv.status_set('maintenance',
                       'Installing kube-controller-manager snap')
    snap.install('kube-controller-manager', channel=channel)
    hookenv.status_set('maintenance', 'Installing kube-scheduler snap')
    snap.install('kube-scheduler', channel=channel)
    hookenv.status_set('maintenance', 'Installing cdk-addons snap')
    snap.install('cdk-addons', channel=channel)
    hookenv.status_set('maintenance', 'Installing kube-proxy snap')
    snap.install('kube-proxy', channel=channel, classic=True)
    calculate_and_store_resource_checksums(checksum_prefix, snap_resources)
    db.set('snap.resources.fingerprint.initialised', True)
    set_state('kubernetes-master.snaps.installed')
    remove_state('kubernetes-master.components.started')


@when('config.changed.client_password', 'leadership.is_leader')
def password_changed():
    """Handle password change via the charms config."""
    password = hookenv.config('client_password')
    if password == "" and is_state('client.password.initialised'):
        # password_changed is called during an upgrade. Nothing to do.
        return
    elif password == "":
        # Password not initialised
        password = token_generator()
    setup_basic_auth(password, "admin", "admin", "system:masters")
    set_state('reconfigure.authentication.setup')
    remove_state('authentication.setup')
    set_state('client.password.initialised')


@when('config.changed.storage-backend')
def storage_backend_changed():
    remove_state('kubernetes-master.components.started')


@when('cni.connected')
@when_not('cni.configured')
def configure_cni(cni):
    ''' Set master configuration on the CNI relation. This lets the CNI
    subordinate know that we're the master so it can respond accordingly. '''
    cni.set_config(is_master=True, kubeconfig_path='')


@when('leadership.is_leader')
@when_not('authentication.setup')
def setup_leader_authentication():
    '''Setup basic authentication and token access for the cluster.'''
    service_key = '/root/cdk/serviceaccount.key'
    basic_auth = '/root/cdk/basic_auth.csv'
    known_tokens = '/root/cdk/known_tokens.csv'

    hookenv.status_set('maintenance', 'Rendering authentication templates.')

    keys = [service_key, basic_auth, known_tokens]
    # Try first to fetch data from an old leadership broadcast.
    if not get_keys_from_leader(keys) \
            or is_state('reconfigure.authentication.setup'):
        last_pass = get_password('basic_auth.csv', 'admin')
        setup_basic_auth(last_pass, 'admin', 'admin', 'system:masters')

        if not os.path.isfile(known_tokens):
            touch(known_tokens)

        # Generate the default service account token key
        os.makedirs('/root/cdk', exist_ok=True)
        if not os.path.isfile(service_key):
            cmd = ['openssl', 'genrsa', '-out', service_key,
                   '2048']
            check_call(cmd)
        remove_state('reconfigure.authentication.setup')

    # read service account key for syndication
    leader_data = {}
    for f in [known_tokens, basic_auth, service_key]:
        with open(f, 'r') as fp:
            leader_data[f] = fp.read()

    # this is slightly opaque, but we are sending file contents under its file
    # path as a key.
    # eg:
    # {'/root/cdk/serviceaccount.key': 'RSA:2471731...'}
    leader_set(leader_data)
    remove_state('kubernetes-master.components.started')
    set_state('authentication.setup')


@when_not('leadership.is_leader')
def setup_non_leader_authentication():

    service_key = '/root/cdk/serviceaccount.key'
    basic_auth = '/root/cdk/basic_auth.csv'
    known_tokens = '/root/cdk/known_tokens.csv'

    keys = [service_key, basic_auth, known_tokens]
    # The source of truth for non-leaders is the leader.
    # Therefore we overwrite_local with whatever the leader has.
    if not get_keys_from_leader(keys, overwrite_local=True):
        # the keys were not retrieved. Non-leaders have to retry.
        return

    if not any_file_changed(keys) and is_state('authentication.setup'):
        # No change detected and we have already setup the authentication
        return

    hookenv.status_set('maintenance', 'Rendering authentication templates.')

    remove_state('kubernetes-master.components.started')
    set_state('authentication.setup')


def get_keys_from_leader(keys, overwrite_local=False):
    """
    Gets the broadcasted keys from the leader and stores them in
    the corresponding files.

    Args:
        keys: list of keys. Keys are actually files on the FS.

    Returns: True if all key were fetched, False if not.

    """
    # This races with other codepaths, and seems to require being created first
    # This block may be extracted later, but for now seems to work as intended
    os.makedirs('/root/cdk', exist_ok=True)

    for k in keys:
        # If the path does not exist, assume we need it
        if not os.path.exists(k) or overwrite_local:
            # Fetch data from leadership broadcast
            contents = leader_get(k)
            # Default to logging the warning and wait for leader data to be set
            if contents is None:
                hookenv.log('Missing content for file {}'.format(k))
                return False
            # Write out the file and move on to the next item
            with open(k, 'w+') as fp:
                fp.write(contents)
                fp.write('\n')

    return True


@when('kubernetes-master.snaps.installed')
def set_app_version():
    ''' Declare the application version to juju '''
    version = check_output(['kube-apiserver', '--version'])
    hookenv.application_version_set(version.split(b' v')[-1].rstrip())


@when('kubernetes-master.snaps.installed')
@when('snap.refresh.set')
@when('leadership.is_leader')
def process_snapd_timer():
    ''' Set the snapd refresh timer on the leader so all cluster members
    (present and future) will refresh near the same time. '''
    # Get the current snapd refresh timer; we know layer-snap has set this
    # when the 'snap.refresh.set' flag is present.
    timer = snap.get(snapname='core', key='refresh.timer').decode('utf-8')

    # The first time through, data_changed will be true. Subsequent calls
    # should only update leader data if something changed.
    if data_changed('master_snapd_refresh', timer):
        hookenv.log('setting snapd_refresh timer to: {}'.format(timer))
        leader_set({'snapd_refresh': timer})


@when('kubernetes-master.snaps.installed')
@when('snap.refresh.set')
@when('leadership.changed.snapd_refresh')
@when_not('leadership.is_leader')
def set_snapd_timer():
    ''' Set the snapd refresh.timer on non-leader cluster members. '''
    # NB: This method should only be run when 'snap.refresh.set' is present.
    # Layer-snap will always set a core refresh.timer, which may not be the
    # same as our leader. Gating with 'snap.refresh.set' ensures layer-snap
    # has finished and we are free to set our config to the leader's timer.
    timer = leader_get('snapd_refresh')
    hookenv.log('setting snapd_refresh timer to: {}'.format(timer))
    snap.set_refresh_timer(timer)


@hookenv.atexit
def set_final_status():
    ''' Set the final status of the charm as we leave hook execution '''
    try:
        goal_state = hookenv.goal_state()
    except NotImplementedError:
        goal_state = {}

    vsphere_joined = is_state('endpoint.vsphere.joined')
    azure_joined = is_state('endpoint.azure.joined')
    cloud_blocked = is_state('kubernetes-master.cloud.blocked')
    if vsphere_joined and cloud_blocked:
        hookenv.status_set('blocked',
                           'vSphere integration requires K8s 1.12 or greater')
        return
    if azure_joined and cloud_blocked:
        hookenv.status_set('blocked',
                           'Azure integration requires K8s 1.11 or greater')
        return

    if is_state('kubernetes-master.cloud.pending'):
        hookenv.status_set('waiting', 'Waiting for cloud integration')
        return

    if not is_state('kube-api-endpoint.available'):
        if 'kube-api-endpoint' in goal_state.get('relations', {}):
            status = 'waiting'
        else:
            status = 'blocked'
        hookenv.status_set(status, 'Waiting for kube-api-endpoint relation')
        return

    if not is_state('kube-control.connected'):
        if 'kube-control' in goal_state.get('relations', {}):
            status = 'waiting'
        else:
            status = 'blocked'
        hookenv.status_set(status, 'Waiting for workers.')
        return

    upgrade_needed = is_state('kubernetes-master.upgrade-needed')
    upgrade_specified = is_state('kubernetes-master.upgrade-specified')
    if upgrade_needed and not upgrade_specified:
        msg = 'Needs manual upgrade, run the upgrade action'
        hookenv.status_set('blocked', msg)
        return

    if is_state('kubernetes-master.components.started'):
        # All services should be up and running at this point. Double-check...
        failing_services = master_services_down()
        if len(failing_services) != 0:
            msg = 'Stopped services: {}'.format(','.join(failing_services))
            hookenv.status_set('blocked', msg)
            return
    else:
        # if we don't have components starting, we're waiting for that and
        # shouldn't fall through to Kubernetes master running.
        if (is_state('cni.available')):
            hookenv.status_set('maintenance',
                               'Waiting for master components to start')
        else:
            hookenv.status_set('waiting',
                               'Waiting for CNI plugins to become available')
        return

    # Note that after this point, kubernetes-master.components.started is
    # always True.
    is_leader = is_state('leadership.is_leader')
    authentication_setup = is_state('authentication.setup')
    if not is_leader and not authentication_setup:
        hookenv.status_set('waiting', "Waiting on leader's crypto keys.")
        return

    addons_configured = is_state('cdk-addons.configured')
    if is_leader and not addons_configured:
        hookenv.status_set('waiting', 'Waiting to retry addon deployment')
        return

    try:
        unready = get_kube_system_pods_not_running()
    except FailedToGetPodStatus:
        hookenv.status_set('waiting', 'Waiting for kube-system pods to start')
        return

    if unready:
        msg = 'Waiting for {} kube-system pod{} to start'
        msg = msg.format(len(unready), "s"[len(unready) == 1:])
        hookenv.status_set('waiting', msg)
        return

    if hookenv.config('service-cidr') != service_cidr():
        msg = 'WARN: cannot change service-cidr, still using ' + service_cidr()
        hookenv.status_set('active', msg)
        return

    gpu_available = is_state('kube-control.gpu.available')
    gpu_enabled = is_state('kubernetes-master.gpu.enabled')
    if gpu_available and not gpu_enabled:
        msg = 'GPUs available. Set allow-privileged="auto" to enable.'
        hookenv.status_set('active', msg)
        return

    hookenv.status_set('active', 'Kubernetes master running.')


def master_services_down():
    """Ensure master services are up and running.

    Return: list of failing services"""
    failing_services = []
    for service in master_services:
        daemon = 'snap.{}.daemon'.format(service)
        if not host.service_running(daemon):
            failing_services.append(service)
    return failing_services


def add_systemd_restart_always():
    template = 'templates/service-always-restart.systemd-latest.conf'

    try:
        # Get the systemd version
        cmd = ['systemd', '--version']
        output = check_output(cmd).decode('UTF-8')
        line = output.splitlines()[0]
        words = line.split()
        assert words[0] == 'systemd'
        systemd_version = int(words[1])

        # Check for old version (for xenial support)
        if systemd_version < 230:
            template = 'templates/service-always-restart.systemd-229.conf'
    except Exception:
        traceback.print_exc()
        hookenv.log('Failed to detect systemd version, using latest template',
                    level='ERROR')

    for service in master_services:
        dest_dir = '/etc/systemd/system/snap.{}.daemon.service.d' \
            .format(service)
        os.makedirs(dest_dir, exist_ok=True)
        copyfile(template, '{}/always-restart.conf'.format(dest_dir))
    check_call(['systemctl', 'daemon-reload'])


@when('etcd.available', 'tls_client.server.certificate.saved',
      'authentication.setup',
      'leadership.set.auto_storage_backend',
      'cni.available')
@when_not('kubernetes-master.components.started',
          'kubernetes-master.cloud.pending',
          'kubernetes-master.cloud.blocked')
def start_master():
    '''Run the Kubernetes master components.'''
    hookenv.status_set('maintenance',
                       'Configuring the Kubernetes master services.')
    freeze_service_cidr()
    etcd = endpoint_from_flag('etcd.available')
    if not etcd.get_connection_string():
        # etcd is not returning a connection string. This happens when
        # the master unit disconnects from etcd and is ready to terminate.
        # No point in trying to start master services and fail. Just return.
        return

    # TODO: Make sure below relation is handled on change
    # https://github.com/kubernetes/kubernetes/issues/43461
    handle_etcd_relation(etcd)

    # make all services restart all the time
    add_systemd_restart_always()

    # Add CLI options to all components
    configure_apiserver(etcd.get_connection_string())
    configure_controller_manager()
    configure_scheduler()

    # kube-proxy
    cni = endpoint_from_flag('cni.available')
    cluster_cidr = cni.get_config()['cidr']
    configure_kube_proxy(configure_prefix,
                         ['127.0.0.1:8080'], cluster_cidr)
    service_restart('snap.kube-proxy.daemon')

    set_state('kubernetes-master.components.started')
    hookenv.open_port(6443)


@when('etcd.available')
def etcd_data_change(etcd):
    ''' Etcd scale events block master reconfiguration due to the
        kubernetes-master.components.started state. We need a way to
        handle these events consistently only when the number of etcd
        units has actually changed '''

    # key off of the connection string
    connection_string = etcd.get_connection_string()

    # If the connection string changes, remove the started state to trigger
    # handling of the master components
    if data_changed('etcd-connect', connection_string):
        remove_state('kubernetes-master.components.started')

    # We are the leader and the auto_storage_backend is not set meaning
    # this is the first time we connect to etcd.
    auto_storage_backend = leader_get('auto_storage_backend')
    is_leader = is_state('leadership.is_leader')
    if is_leader and not auto_storage_backend:
        if etcd.get_version().startswith('3.'):
            leader_set(auto_storage_backend='etcd3')
        else:
            leader_set(auto_storage_backend='etcd2')


@when('kube-control.connected')
@when('cdk-addons.configured')
def send_cluster_dns_detail(kube_control):
    ''' Send cluster DNS info '''
    enableKubeDNS = hookenv.config('enable-kube-dns')
    dnsDomain = hookenv.config('dns_domain')
    dns_ip = None
    if enableKubeDNS:
        try:
            dns_ip = get_dns_ip()
        except CalledProcessError:
            hookenv.log("kubedns not ready yet")
            return
    kube_control.set_dns(53, dnsDomain, dns_ip, enableKubeDNS)


@when('kube-control.connected')
@when('snap.installed.kubectl')
@when('leadership.is_leader')
def create_service_configs(kube_control):
    """Create the users for kubelet"""
    should_restart = False
    # generate the username/pass for the requesting unit
    proxy_token = get_token('system:kube-proxy')
    if not proxy_token:
        setup_tokens(None, 'system:kube-proxy', 'kube-proxy')
        proxy_token = get_token('system:kube-proxy')
        should_restart = True

    client_token = get_token('admin')
    if not client_token:
        setup_tokens(None, 'admin', 'admin', "system:masters")
        client_token = get_token('admin')
        should_restart = True

    requests = kube_control.auth_user()
    for request in requests:
        username = request[1]['user']
        group = request[1]['group']
        if not username or not group:
            continue
        kubelet_token = get_token(username)
        if not kubelet_token:
            # Usernames have to be in the form of system:node:<nodeName>
            userid = "kubelet-{}".format(request[0].split('/')[1])
            setup_tokens(None, username, userid, group)
            kubelet_token = get_token(username)
            should_restart = True
        kube_control.sign_auth_request(request[0], username,
                                       kubelet_token, proxy_token,
                                       client_token)

    if should_restart:
        service_restart('snap.kube-apiserver.daemon')
        remove_state('authentication.setup')


@when('kube-api-endpoint.available')
def push_service_data(kube_api):
    ''' Send configuration to the load balancer, and close access to the
    public interface '''
    kube_api.configure(port=6443)


@when('certificates.available', 'kube-api-endpoint.available')
def send_data(tls, kube_api_endpoint):
    '''Send the data that is required to create a server certificate for
    this server.'''
    # Use the public ip of this unit as the Common Name for the certificate.
    common_name = hookenv.unit_public_ip()

    # Get the SDN gateway based on the cidr address.
    kubernetes_service_ip = get_kubernetes_service_ip()

    # Get ingress address
    ingress_ip = get_ingress_address(kube_api_endpoint.endpoint_name)

    domain = hookenv.config('dns_domain')
    # Create SANs that the tls layer will add to the server cert.
    sans = [
        hookenv.unit_public_ip(),
        ingress_ip,
        socket.gethostname(),
        kubernetes_service_ip,
        'kubernetes',
        'kubernetes.{0}'.format(domain),
        'kubernetes.default',
        'kubernetes.default.svc',
        'kubernetes.default.svc.{0}'.format(domain)
    ]

    # maybe they have extra names they want as SANs
    extra_sans = hookenv.config('extra_sans')
    if extra_sans and not extra_sans == "":
        sans.extend(extra_sans.split())

    # Create a path safe name by removing path characters from the unit name.
    certificate_name = hookenv.local_unit().replace('/', '_')
    # Request a server cert with this information.
    tls.request_server_cert(common_name, sans, certificate_name)


@when('config.changed.extra_sans', 'certificates.available',
      'kube-api-endpoint.available')
def update_certificate(tls, kube_api_endpoint):
    # Using the config.changed.extra_sans flag to catch changes.
    # IP changes will take ~5 minutes or so to propagate, but
    # it will update.
    send_data(tls, kube_api_endpoint)


@when('certificates.server.cert.available',
      'kubernetes-master.components.started',
      'tls_client.server.certificate.written')
def kick_api_server(tls):
    # need to be idempotent and don't want to kick the api server
    # without need
    if data_changed('cert', tls.get_server_cert()):
        # certificate changed, so restart the api server
        hookenv.log("Certificate information changed, restarting api server")
        service_restart('snap.kube-apiserver.daemon')
    tls_client.reset_certificate_write_flag('server')


@when_any('config.changed.keystone-policy',
          'config.changed.keystone-ssl-ca')
@when('keystone-credentials.available.auth')
def regenerate_cdk_addons():
    configure_cdk_addons()


@when_any('kubernetes-master.components.started',
          'kubernetes-master.ceph.configured',
          'keystone-credentials.available.auth')
@when('leadership.is_leader')
def configure_cdk_addons():
    ''' Configure CDK addons '''
    remove_state('cdk-addons.configured')
    load_gpu_plugin = hookenv.config('enable-nvidia-plugin').lower()
    gpuEnable = (get_version('kube-apiserver') >= (1, 9) and
                 load_gpu_plugin == "auto" and
                 is_state('kubernetes-master.gpu.enabled'))
    registry = hookenv.config('addons-registry')
    dbEnabled = str(hookenv.config('enable-dashboard-addons')).lower()
    dnsEnabled = str(hookenv.config('enable-kube-dns')).lower()
    metricsEnabled = str(hookenv.config('enable-metrics')).lower()
    default_storage = ''
    ceph = {}
    if (is_state('kubernetes-master.ceph.configured') and
            get_version('kube-apiserver') >= (1, 12)):
        cephEnabled = "true"
        ceph_ep = endpoint_from_flag('ceph-storage.available')
        b64_ceph_key = base64.b64encode(ceph_ep.key().encode('utf-8'))
        ceph['admin_key'] = b64_ceph_key.decode('ascii')
        ceph['kubernetes_key'] = b64_ceph_key.decode('ascii')
        ceph['mon_hosts'] = ceph_ep.mon_hosts()
        default_storage = hookenv.config('default-storage')
    else:
        cephEnabled = "false"

    keystone = {}
    ks = endpoint_from_flag('keystone-credentials.available.auth')
    if ks:
        keystoneEnabled = "true"
        keystone['cert'] = '/root/cdk/server.crt'
        keystone['key'] = '/root/cdk/server.key'
        keystone['url'] = '{}://{}:{}/v{}'.format(ks.auth_protocol(),
                                                  ks.auth_host(),
                                                  ks.auth_port(),
                                                  ks.api_version())
        keystone['keystone-ca'] = hookenv.config('keystone-ssl-ca')
    else:
        keystoneEnabled = "false"

    args = [
        'arch=' + arch(),
        'dns-ip=' + get_deprecated_dns_ip(),
        'dns-domain=' + hookenv.config('dns_domain'),
        'registry=' + registry,
        'enable-dashboard=' + dbEnabled,
        'enable-kube-dns=' + dnsEnabled,
        'enable-metrics=' + metricsEnabled,
        'enable-gpu=' + str(gpuEnable).lower(),
        'enable-ceph=' + cephEnabled,
        'ceph-admin-key=' + (ceph.get('admin_key', '')),
        'ceph-kubernetes-key=' + (ceph.get('admin_key', '')),
        'ceph-mon-hosts="' + (ceph.get('mon_hosts', '')) + '"',
        'default-storage=' + default_storage,
        'enable-keystone=' + keystoneEnabled,
        'keystone-cert-file=' + keystone.get('cert', ''),
        'keystone-key-file=' + keystone.get('key', ''),
        'keystone-server-url=' + keystone.get('url', ''),
        'keystone-server-ca=' + keystone.get('keystone-ca', '')
    ]
    check_call(['snap', 'set', 'cdk-addons'] + args)
    if not addons_ready():
        remove_state('cdk-addons.configured')
        return

    set_state('cdk-addons.configured')
    if ks:
        leader_set({'keystone-cdk-addons-configured': True})
    else:
        leader_set({'keystone-cdk-addons-configured': None})


@retry(times=3, delay_secs=20)
def addons_ready():
    """
    Test if the add ons got installed

    Returns: True is the addons got applied

    """
    try:
        check_call(['cdk-addons.apply'])
        return True
    except CalledProcessError:
        hookenv.log("Addons are not ready yet.")
        return False


@when('loadbalancer.available', 'certificates.ca.available',
      'certificates.client.cert.available', 'authentication.setup')
def loadbalancer_kubeconfig(loadbalancer, ca, client):
    # Get the potential list of loadbalancers from the relation object.
    hosts = loadbalancer.get_addresses_ports()
    # Get the public address of loadbalancers so users can access the cluster.
    address = hosts[0].get('public-address')
    # Get the port of the loadbalancer so users can access the cluster.
    port = hosts[0].get('port')
    server = 'https://{0}:{1}'.format(address, port)
    build_kubeconfig(server)


@when('certificates.ca.available', 'certificates.client.cert.available',
      'authentication.setup')
@when_not('loadbalancer.available')
def create_self_config(ca, client):
    '''Create a kubernetes configuration for the master unit.'''
    server = 'https://{0}:{1}'.format(hookenv.unit_get('public-address'), 6443)
    build_kubeconfig(server)


@when('ceph-storage.available')
def ceph_state_control(ceph_admin):
    ''' Determine if we should remove the state that controls the re-render
    and execution of the ceph-relation-changed event because there
    are changes in the relationship data, and we should re-render any
    configs, keys, and/or service pre-reqs '''

    ceph_relation_data = {
        'mon_hosts': ceph_admin.mon_hosts(),
        'fsid': ceph_admin.fsid(),
        'auth_supported': ceph_admin.auth(),
        'hostname': socket.gethostname(),
        'key': ceph_admin.key()
    }

    # Re-execute the rendering if the data has changed.
    if data_changed('ceph-config', ceph_relation_data):
        remove_state('kubernetes-master.ceph.configured')


@when('ceph-storage.available')
@when_not('kubernetes-master.ceph.configured')
def ceph_storage():
    '''Ceph on kubernetes will require a few things - namely a ceph
    configuration, and the ceph secret key file used for authentication.
    This method will install the client package, and render the requisit files
    in order to consume the ceph-storage relation.'''

    ceph_admin = endpoint_from_flag('ceph-storage.available')

    # deprecated in 1.12 in favor of using CSI instead of dumping the config
    # to ceph. Also be sure to note that we don't set
    # kubernetes-master.ceph.configured until ceph is up and has provided
    # us a key.
    if get_version('kube-apiserver') >= (1, 12):
        # this is actually false, but by setting this flag we won't keep
        # running this function for no reason. Also note that we watch this
        # flag to run cdk-addons.apply.
        if not ceph_admin.key():
            # We didn't have a key, and cannot proceed. Do not set state and
            # allow this method to re-execute
            return

        set_state('kubernetes-master.ceph.configured')
        return

    ceph_context = {
        'mon_hosts': ceph_admin.mon_hosts(),
        'fsid': ceph_admin.fsid(),
        'auth_supported': ceph_admin.auth(),
        'use_syslog': "true",
        'ceph_public_network': '',
        'ceph_cluster_network': '',
        'loglevel': 1,
        'hostname': socket.gethostname(),
    }
    # Install the ceph common utilities.
    apt_install(['ceph-common'], fatal=True)

    etc_ceph_directory = '/etc/ceph'
    if not os.path.isdir(etc_ceph_directory):
        os.makedirs(etc_ceph_directory)
    charm_ceph_conf = os.path.join(etc_ceph_directory, 'ceph.conf')
    # Render the ceph configuration from the ceph conf template
    render('ceph.conf', charm_ceph_conf, ceph_context)

    # The key can rotate independently of other ceph config, so validate it
    admin_key = os.path.join(etc_ceph_directory,
                             'ceph.client.admin.keyring')
    try:
        with open(admin_key, 'w') as key_file:
            key_file.write("[client.admin]\n\tkey = {}\n".format(
                ceph_admin.key()))
    except IOError as err:
        hookenv.log("IOError writing admin.keyring: {}".format(err))

    # Enlist the ceph-admin key as a kubernetes secret
    if ceph_admin.key():
        encoded_key = base64.b64encode(ceph_admin.key().encode('utf-8'))
    else:
        # We didn't have a key, and cannot proceed. Do not set state and
        # allow this method to re-execute
        return

    context = {'secret': encoded_key.decode('ascii')}
    render('ceph-secret.yaml', '/tmp/ceph-secret.yaml', context)
    try:
        # At first glance this is deceptive. The apply stanza will create if
        # it doesn't exist, otherwise it will update the entry, ensuring our
        # ceph-secret is always reflective of what we have in /etc/ceph
        # assuming we have invoked this anytime that file would change.
        cmd = ['kubectl', 'apply', '-f', '/tmp/ceph-secret.yaml']
        check_call(cmd)
        os.remove('/tmp/ceph-secret.yaml')
    except:  # NOQA
        # the enlistment in kubernetes failed, return and prepare for re-exec
        return

    # when complete, set a state relating to configuration of the storage
    # backend that will allow other modules to hook into this and verify we
    # have performed the necessary pre-req steps to interface with a ceph
    # deployment.
    set_state('kubernetes-master.ceph.configured')


@when('nrpe-external-master.available')
@when_not('nrpe-external-master.initial-config')
def initial_nrpe_config(nagios=None):
    set_state('nrpe-external-master.initial-config')
    update_nrpe_config(nagios)


@when('config.changed.authorization-mode')
def switch_auth_mode():
    config = hookenv.config()
    mode = config.get('authorization-mode')

    if data_changed('auth-mode', mode):
        # manage flags to handle rbac related resources
        if mode and 'rbac' in mode.lower():
            remove_state('kubernetes-master.remove.rbac')
            set_state('kubernetes-master.create.rbac')
        else:
            remove_state('kubernetes-master.create.rbac')
            set_state('kubernetes-master.remove.rbac')

        # set ourselves up to restart since auth mode has changed
        remove_state('kubernetes-master.components.started')


@when('leadership.is_leader',
      'kubernetes-master.components.started',
      'kubernetes-master.create.rbac')
def create_rbac_resources():
    rbac_proxy_path = '/root/cdk/rbac-proxy.yaml'

    # NB: when metrics and logs are retrieved by proxy, the 'user' is the
    # common name of the cert used to authenticate the proxied request.
    # The CN for /root/cdk/client.crt is 'client'.
    proxy_user = 'client'
    context = {'juju_application': hookenv.service_name(),
               'proxy_user': proxy_user}
    render('rbac-proxy.yaml', rbac_proxy_path, context)

    hookenv.log('Creating proxy-related RBAC resources.')
    if kubectl_manifest('apply', rbac_proxy_path):
        remove_state('kubernetes-master.create.rbac')
    else:
        msg = 'Failed to apply {}, will retry.'.format(rbac_proxy_path)
        hookenv.log(msg)


@when('leadership.is_leader',
      'kubernetes-master.components.started',
      'kubernetes-master.remove.rbac')
def remove_rbac_resources():
    rbac_proxy_path = '/root/cdk/rbac-proxy.yaml'
    if os.path.isfile(rbac_proxy_path):
        hookenv.log('Removing proxy-related RBAC resources.')
        if kubectl_manifest('delete', rbac_proxy_path):
            os.remove(rbac_proxy_path)
            remove_state('kubernetes-master.remove.rbac')
        else:
            msg = 'Failed to delete {}, will retry.'.format(rbac_proxy_path)
            hookenv.log(msg)
    else:
        # if we dont have the yaml, there's nothing for us to do
        remove_state('kubernetes-master.remove.rbac')


@when('kubernetes-master.components.started')
@when('nrpe-external-master.available')
@when_any('config.changed.nagios_context',
          'config.changed.nagios_servicegroups')
def update_nrpe_config(unused=None):
    services = (
        'snap.kube-apiserver.daemon',
        'snap.kube-controller-manager.daemon',
        'snap.kube-scheduler.daemon'
    )
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe.add_init_service_checks(nrpe_setup, services, current_unit)
    nrpe_setup.write()


@when_not('nrpe-external-master.available')
@when('nrpe-external-master.initial-config')
def remove_nrpe_config(nagios=None):
    remove_state('nrpe-external-master.initial-config')

    # List of systemd services for which the checks will be removed
    services = (
        'snap.kube-apiserver.daemon',
        'snap.kube-controller-manager.daemon',
        'snap.kube-scheduler.daemon'
    )

    # The current nrpe-external-master interface doesn't handle a lot of logic,
    # use the charm-helpers code for now.
    hostname = nrpe.get_nagios_hostname()
    nrpe_setup = nrpe.NRPE(hostname=hostname)

    for service in services:
        nrpe_setup.remove_check(shortname=service)


def is_privileged():
    """Return boolean indicating whether or not to set allow-privileged=true.

    """
    privileged = hookenv.config('allow-privileged').lower()
    if privileged == 'auto':
        return is_state('kubernetes-master.gpu.enabled')
    else:
        return privileged == 'true'


@when('config.changed.allow-privileged')
@when('kubernetes-master.components.started')
def on_config_allow_privileged_change():
    """React to changed 'allow-privileged' config value.

    """
    remove_state('kubernetes-master.components.started')
    remove_state('config.changed.allow-privileged')


@when_any('config.changed.api-extra-args',
          'config.changed.audit-policy',
          'config.changed.audit-webhook-config',
          'config.changed.enable-keystone-authorization')
@when('kubernetes-master.components.started')
@when('leadership.set.auto_storage_backend')
@when('etcd.available')
def reconfigure_apiserver(etcd):
    configure_apiserver(etcd.get_connection_string())


@when('config.changed.controller-manager-extra-args')
@when('kubernetes-master.components.started')
def on_config_controller_manager_extra_args_change():
    configure_controller_manager()


@when('config.changed.scheduler-extra-args')
@when('kubernetes-master.components.started')
def on_config_scheduler_extra_args_change():
    configure_scheduler()


@when('kube-control.gpu.available')
@when('kubernetes-master.components.started')
@when_not('kubernetes-master.gpu.enabled')
def on_gpu_available(kube_control):
    """The remote side (kubernetes-worker) is gpu-enabled.

    We need to run in privileged mode.

    """
    kube_version = get_version('kube-apiserver')
    config = hookenv.config()
    if (config['allow-privileged'].lower() == "false" and
            kube_version < (1, 9)):
        return

    remove_state('kubernetes-master.components.started')
    set_state('kubernetes-master.gpu.enabled')


@when('kubernetes-master.gpu.enabled')
@when('kubernetes-master.components.started')
@when_not('kubernetes-master.privileged')
def gpu_with_no_privileged():
    """We were in gpu mode, but the operator has set allow-privileged="false",
    so we can't run in gpu mode anymore.

    """
    if get_version('kube-apiserver') < (1, 9):
        remove_state('kubernetes-master.gpu.enabled')


@when('kube-control.connected')
@when_not('kube-control.gpu.available')
@when('kubernetes-master.gpu.enabled')
@when('kubernetes-master.components.started')
def gpu_departed(kube_control):
    """We were in gpu mode, but the workers informed us there is
    no gpu support anymore.

    """
    remove_state('kubernetes-master.gpu.enabled')


@hook('stop')
def shutdown():
    """ Stop the kubernetes master services

    """
    for service in master_services:
        service_stop('snap.%s.daemon' % service)


def build_kubeconfig(server):
    '''Gather the relevant data for Kubernetes configuration objects and create
    a config object with that information.'''
    # Get the options from the tls-client layer.
    layer_options = layer.options('tls-client')
    # Get all the paths to the tls information required for kubeconfig.
    ca = layer_options.get('ca_certificate_path')
    ca_exists = ca and os.path.isfile(ca)
    client_pass = get_password('basic_auth.csv', 'admin')
    # Do we have everything we need?
    if ca_exists and client_pass:
        # drop keystone helper script?
        ks = endpoint_from_flag('keystone-credentials.available.auth')
        if ks:
            script_filename = 'kube-keystone.sh'
            keystone_path = os.path.join(os.sep, 'home', 'ubuntu',
                                         script_filename)
            context = {'protocol': ks.credentials_protocol(),
                       'address': ks.credentials_host(),
                       'port': ks.credentials_port(),
                       'version': ks.api_version()}
            render(script_filename, keystone_path, context)
        elif is_state('leadership.set.keystone-cdk-addons-configured'):
            # if addons are configured, we're going to do keystone
            # just not yet because we don't have creds
            hookenv.log('Keystone endpoint not found, will retry.')

        # Create an absolute path for the kubeconfig file.
        kubeconfig_path = os.path.join(os.sep, 'home', 'ubuntu', 'config')
        # Create the kubeconfig on this system so users can access the cluster.

        if ks:
            create_kubeconfig(kubeconfig_path, server, ca,
                              user='admin', password=client_pass,
                              keystone=True)
        else:
            create_kubeconfig(kubeconfig_path, server, ca,
                              user='admin', password=client_pass)

        # Make the config file readable by the ubuntu users so juju scp works.
        cmd = ['chown', 'ubuntu:ubuntu', kubeconfig_path]
        check_call(cmd)

        # make a copy in a location shared by kubernetes-worker
        # and kubernete-master
        create_kubeconfig(kubeclientconfig_path, server, ca,
                          user='admin', password=client_pass)

        # make a kubeconfig for kube-proxy
        proxy_token = get_token('system:kube-proxy')
        if not proxy_token:
            setup_tokens(None, 'system:kube-proxy', 'kube-proxy')
            proxy_token = get_token('system:kube-proxy')
        create_kubeconfig(kubeproxyconfig_path, server, ca,
                          token=proxy_token, user='kube-proxy')


def get_dns_ip():
    return get_service_ip('kube-dns', namespace='kube-system')


def get_deprecated_dns_ip():
    '''We previously hardcoded the dns ip. This function returns the old
    hardcoded value for use with older versions of cdk_addons.'''
    interface = ipaddress.IPv4Interface(service_cidr())
    ip = interface.network.network_address + 10
    return ip.exploded


def get_kubernetes_service_ip():
    '''Get the IP address for the kubernetes service based on the cidr.'''
    interface = ipaddress.IPv4Interface(service_cidr())
    # Add .1 at the end of the network
    ip = interface.network.network_address + 1
    return ip.exploded


def handle_etcd_relation(reldata):
    ''' Save the client credentials and set appropriate daemon flags when
    etcd declares itself as available'''
    # Define where the etcd tls files will be kept.
    etcd_dir = '/root/cdk/etcd'

    # Create paths to the etcd client ca, key, and cert file locations.
    ca = os.path.join(etcd_dir, 'client-ca.pem')
    key = os.path.join(etcd_dir, 'client-key.pem')
    cert = os.path.join(etcd_dir, 'client-cert.pem')

    # Save the client credentials (in relation data) to the paths provided.
    reldata.save_client_credentials(key, cert, ca)


def remove_if_exists(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def write_file_with_autogenerated_header(path, contents):
    with open(path, 'w') as f:
        header = '# Autogenerated by kubernetes-master charm'
        f.write(header + '\n' + contents)


def configure_apiserver(etcd_connection_string):
    api_opts = {}

    # Get the tls paths from the layer data.
    layer_options = layer.options('tls-client')
    ca_cert_path = layer_options.get('ca_certificate_path')
    client_cert_path = layer_options.get('client_certificate_path')
    client_key_path = layer_options.get('client_key_path')
    server_cert_path = layer_options.get('server_certificate_path')
    server_key_path = layer_options.get('server_key_path')

    # at one point in time, this code would set ca-client-cert,
    # but this was removed. This was before configure_kubernetes_service
    # kept track of old arguments and removed them, so client-ca-cert
    # was able to hang around forever stored in the snap configuration.
    # This removes that stale configuration from the snap if it still
    # exists.
    api_opts['client-ca-file'] = 'null'

    if is_privileged():
        api_opts['allow-privileged'] = 'true'
        set_state('kubernetes-master.privileged')
    else:
        api_opts['allow-privileged'] = 'false'
        remove_state('kubernetes-master.privileged')

    # Handle static options for now
    api_opts['service-cluster-ip-range'] = service_cidr()
    api_opts['min-request-timeout'] = '300'
    api_opts['v'] = '4'
    api_opts['tls-cert-file'] = server_cert_path
    api_opts['tls-private-key-file'] = server_key_path
    api_opts['kubelet-certificate-authority'] = ca_cert_path
    api_opts['kubelet-client-certificate'] = client_cert_path
    api_opts['kubelet-client-key'] = client_key_path
    api_opts['logtostderr'] = 'true'
    api_opts['insecure-bind-address'] = '127.0.0.1'
    api_opts['insecure-port'] = '8080'
    api_opts['storage-backend'] = getStorageBackend()
    api_opts['basic-auth-file'] = '/root/cdk/basic_auth.csv'

    api_opts['token-auth-file'] = '/root/cdk/known_tokens.csv'
    api_opts['service-account-key-file'] = '/root/cdk/serviceaccount.key'
    api_opts['kubelet-preferred-address-types'] = \
        '[InternalIP,Hostname,InternalDNS,ExternalDNS,ExternalIP]'
    api_opts['advertise-address'] = get_ingress_address('kube-control')

    etcd_dir = '/root/cdk/etcd'
    etcd_ca = os.path.join(etcd_dir, 'client-ca.pem')
    etcd_key = os.path.join(etcd_dir, 'client-key.pem')
    etcd_cert = os.path.join(etcd_dir, 'client-cert.pem')

    api_opts['etcd-cafile'] = etcd_ca
    api_opts['etcd-keyfile'] = etcd_key
    api_opts['etcd-certfile'] = etcd_cert
    api_opts['etcd-servers'] = etcd_connection_string

    admission_control_pre_1_9 = [
        'NamespaceLifecycle',
        'LimitRanger',
        'ServiceAccount',
        'ResourceQuota',
        'DefaultTolerationSeconds'
    ]

    admission_control = [
        'NamespaceLifecycle',
        'LimitRanger',
        'ServiceAccount',
        'PersistentVolumeLabel',
        'DefaultStorageClass',
        'DefaultTolerationSeconds',
        'MutatingAdmissionWebhook',
        'ValidatingAdmissionWebhook',
        'ResourceQuota'
    ]

    auth_mode = hookenv.config('authorization-mode')
    if 'Node' in auth_mode:
        admission_control.append('NodeRestriction')

    ks = endpoint_from_flag('keystone-credentials.available.auth')
    ks_ip = None
    if ks:
        ks_ip = get_service_ip('k8s-keystone-auth-service', errors_fatal=False)
    if ks and ks_ip:
        os.makedirs(keystone_root, exist_ok=True)

        keystone_webhook = keystone_root + '/webhook.yaml'
        context = {}
        context['keystone_service_cluster_ip'] = ks_ip
        render('keystone-api-server-webhook.yaml', keystone_webhook, context)
        api_opts['authentication-token-webhook-config-file'] = keystone_webhook

        if hookenv.config('enable-keystone-authorization'):
            # if user wants authorization, enable it
            if 'Webhook' not in auth_mode:
                auth_mode += ",Webhook"
            api_opts['authorization-webhook-config-file'] = keystone_webhook
        set_state('keystone.apiserver.configured')
    else:
        if ks and not ks_ip:
            hookenv.log('Unable to find k8s-keystone-auth-service '
                        'service. Will retry')
        elif is_state('leadership.set.keystone-cdk-addons-configured'):
            hookenv.log('Unable to find keystone endpoint. Will retry')
        remove_state('keystone.apiserver.configured')

    api_opts['authorization-mode'] = auth_mode

    kube_version = get_version('kube-apiserver')
    if kube_version < (1, 6):
        hookenv.log('Removing DefaultTolerationSeconds from admission-control')
        admission_control_pre_1_9.remove('DefaultTolerationSeconds')
    if kube_version < (1, 9):
        api_opts['admission-control'] = ','.join(admission_control_pre_1_9)
    else:
        api_opts['admission-control'] = ','.join(admission_control)

    if kube_version > (1, 6) and \
       hookenv.config('enable-metrics'):
        api_opts['requestheader-client-ca-file'] = ca_cert_path
        api_opts['requestheader-allowed-names'] = 'client'
        api_opts['requestheader-extra-headers-prefix'] = 'X-Remote-Extra-'
        api_opts['requestheader-group-headers'] = 'X-Remote-Group'
        api_opts['requestheader-username-headers'] = 'X-Remote-User'
        api_opts['proxy-client-cert-file'] = client_cert_path
        api_opts['proxy-client-key-file'] = client_key_path
        api_opts['enable-aggregator-routing'] = 'true'
        api_opts['client-ca-file'] = ca_cert_path

    api_cloud_config_path = cloud_config_path('kube-apiserver')
    if is_state('endpoint.aws.ready'):
        api_opts['cloud-provider'] = 'aws'
    elif is_state('endpoint.gcp.ready'):
        api_opts['cloud-provider'] = 'gce'
        api_opts['cloud-config'] = str(api_cloud_config_path)
    elif is_state('endpoint.openstack.ready'):
        api_opts['cloud-provider'] = 'openstack'
        api_opts['cloud-config'] = str(api_cloud_config_path)
    elif (is_state('endpoint.vsphere.ready') and
          get_version('kube-apiserver') >= (1, 12)):
        api_opts['cloud-provider'] = 'vsphere'
        api_opts['cloud-config'] = str(api_cloud_config_path)
    elif is_state('endpoint.azure.ready'):
        api_opts['cloud-provider'] = 'azure'
        api_opts['cloud-config'] = str(api_cloud_config_path)

    audit_root = '/root/cdk/audit'
    os.makedirs(audit_root, exist_ok=True)

    audit_log_path = audit_root + '/audit.log'
    api_opts['audit-log-path'] = audit_log_path
    api_opts['audit-log-maxsize'] = '100'
    api_opts['audit-log-maxbackup'] = '9'

    audit_policy_path = audit_root + '/audit-policy.yaml'
    audit_policy = hookenv.config('audit-policy')
    if audit_policy:
        write_file_with_autogenerated_header(audit_policy_path, audit_policy)
        api_opts['audit-policy-file'] = audit_policy_path
    else:
        remove_if_exists(audit_policy_path)

    audit_webhook_config_path = audit_root + '/audit-webhook-config.yaml'
    audit_webhook_config = hookenv.config('audit-webhook-config')
    if audit_webhook_config:
        write_file_with_autogenerated_header(audit_webhook_config_path,
                                             audit_webhook_config)
        api_opts['audit-webhook-config-file'] = audit_webhook_config_path
    else:
        remove_if_exists(audit_webhook_config_path)

    configure_kubernetes_service(configure_prefix, 'kube-apiserver',
                                 api_opts, 'api-extra-args')
    service_restart('snap.kube-apiserver.daemon')


def configure_controller_manager():
    controller_opts = {}

    # Get the tls paths from the layer data.
    layer_options = layer.options('tls-client')
    ca_cert_path = layer_options.get('ca_certificate_path')
    server_cert_path = layer_options.get('server_certificate_path')
    server_key_path = layer_options.get('server_key_path')

    # Default to 3 minute resync. TODO: Make this configurable?
    controller_opts['min-resync-period'] = '3m'
    controller_opts['v'] = '2'
    controller_opts['root-ca-file'] = ca_cert_path
    controller_opts['logtostderr'] = 'true'
    controller_opts['master'] = 'http://127.0.0.1:8080'
    controller_opts['service-account-private-key-file'] = \
        '/root/cdk/serviceaccount.key'
    controller_opts['tls-cert-file'] = server_cert_path
    controller_opts['tls-private-key-file'] = server_key_path

    cm_cloud_config_path = cloud_config_path('kube-controller-manager')
    if is_state('endpoint.aws.ready'):
        controller_opts['cloud-provider'] = 'aws'
    elif is_state('endpoint.gcp.ready'):
        controller_opts['cloud-provider'] = 'gce'
        controller_opts['cloud-config'] = str(cm_cloud_config_path)
    elif is_state('endpoint.openstack.ready'):
        controller_opts['cloud-provider'] = 'openstack'
        controller_opts['cloud-config'] = str(cm_cloud_config_path)
    elif (is_state('endpoint.vsphere.ready') and
          get_version('kube-apiserver') >= (1, 12)):
        controller_opts['cloud-provider'] = 'vsphere'
        controller_opts['cloud-config'] = str(cm_cloud_config_path)
    elif is_state('endpoint.azure.ready'):
        controller_opts['cloud-provider'] = 'azure'
        controller_opts['cloud-config'] = str(cm_cloud_config_path)

    configure_kubernetes_service(configure_prefix, 'kube-controller-manager',
                                 controller_opts,
                                 'controller-manager-extra-args')
    service_restart('snap.kube-controller-manager.daemon')


def configure_scheduler():
    scheduler_opts = {}

    scheduler_opts['v'] = '2'
    scheduler_opts['logtostderr'] = 'true'
    scheduler_opts['master'] = 'http://127.0.0.1:8080'

    configure_kubernetes_service(configure_prefix, 'kube-scheduler',
                                 scheduler_opts, 'scheduler-extra-args')

    service_restart('snap.kube-scheduler.daemon')


def setup_basic_auth(password=None, username='admin', uid='admin',
                     groups=None):
    '''Create the htacces file and the tokens.'''
    root_cdk = '/root/cdk'
    if not os.path.isdir(root_cdk):
        os.makedirs(root_cdk)
    htaccess = os.path.join(root_cdk, 'basic_auth.csv')
    if not password:
        password = token_generator()
    with open(htaccess, 'w') as stream:
        if groups:
            stream.write('{0},{1},{2},"{3}"'.format(password,
                                                    username, uid, groups))
        else:
            stream.write('{0},{1},{2}'.format(password, username, uid))


def setup_tokens(token, username, user, groups=None):
    '''Create a token file for kubernetes authentication.'''
    root_cdk = '/root/cdk'
    if not os.path.isdir(root_cdk):
        os.makedirs(root_cdk)
    known_tokens = os.path.join(root_cdk, 'known_tokens.csv')
    if not token:
        token = token_generator()
    with open(known_tokens, 'a') as stream:
        if groups:
            stream.write('{0},{1},{2},"{3}"\n'.format(token,
                                                      username,
                                                      user,
                                                      groups))
        else:
            stream.write('{0},{1},{2}\n'.format(token, username, user))


def get_password(csv_fname, user):
    '''Get the password of user within the csv file provided.'''
    root_cdk = '/root/cdk'
    tokens_fname = os.path.join(root_cdk, csv_fname)
    if not os.path.isfile(tokens_fname):
        return None
    with open(tokens_fname, 'r') as stream:
        for line in stream:
            record = line.split(',')
            if record[1] == user:
                return record[0]
    return None


def get_token(username):
    """Grab a token from the static file if present. """
    return get_password('known_tokens.csv', username)


def set_token(password, save_salt):
    ''' Store a token so it can be recalled later by token_generator.

    param: password - the password to be stored
    param: save_salt - the key to store the value of the token.'''
    db.set(save_salt, password)
    return db.get(save_salt)


def token_generator(length=32):
    ''' Generate a random token for use in passwords and account tokens.

    param: length - the length of the token to generate'''
    alpha = string.ascii_letters + string.digits
    token = ''.join(random.SystemRandom().choice(alpha) for _ in range(length))
    return token


@retry(times=3, delay_secs=10)
def get_pods(namespace='default'):
    cmd = ['kubectl', 'get', 'po', '-n', namespace, '-o', 'json']
    try:
        output = check_output(cmd).decode('utf-8')
        result = json.loads(output)
    except CalledProcessError:
        hookenv.log('failed to get {} pod status'.format(namespace))
        return None
    return result


class FailedToGetPodStatus(Exception):
    pass


def get_kube_system_pods_not_running():
    ''' Check pod status in the kube-system namespace. Throws
    FailedToGetPodStatus if unable to determine pod status. This can
    occur when the api server is not currently running. On success,
    returns a list of pods that are not currently running
    or an empty list if all are running.'''

    result = get_pods('kube-system')
    if result is None:
        raise FailedToGetPodStatus

    hookenv.log('Checking system pods status: {}'.format(', '.join(
        '='.join([pod['metadata']['name'], pod['status']['phase']])
        for pod in result['items'])))

    # Pods that are Running or Evicted (which should re-spawn) are
    # considered running
    not_running = [pod for pod in result['items']
                   if pod['status']['phase'] != 'Running'
                   and pod['status'].get('reason', '') != 'Evicted']

    pending = [pod for pod in result['items']
               if pod['status']['phase'] == 'Pending']
    all_pending = len(pending) == len(result['items'])
    if is_state('endpoint.gcp.ready') and all_pending:
        poke_network_unavailable()
        return not_running

    return not_running


def poke_network_unavailable():
    """
    Work around https://github.com/kubernetes/kubernetes/issues/44254 by
    manually poking the status into the API server to tell the nodes they have
    a network route.

    This is needed because kubelet sets the NetworkUnavailable flag and expects
    the network plugin to clear it, which only kubenet does. There is some
    discussion about refactoring the affected code but nothing has happened
    in a while.
    """
    cmd = ['kubectl', 'get', 'nodes', '-o', 'json']

    try:
        output = check_output(cmd).decode('utf-8')
        nodes = json.loads(output)['items']
    except CalledProcessError:
        hookenv.log('failed to get kube-system nodes')
        return
    except (KeyError, json.JSONDecodeError) as e:
        hookenv.log('failed to parse kube-system node status '
                    '({}): {}'.format(e, output), hookenv.ERROR)
        return

    for node in nodes:
        node_name = node['metadata']['name']
        url = 'http://localhost:8080/api/v1/nodes/{}/status'.format(node_name)
        with urlopen(url) as response:
            code = response.getcode()
            body = response.read().decode('utf8')
        if code != 200:
            hookenv.log('failed to get node status from {} [{}]: {}'.format(
                url, code, body), hookenv.ERROR)
            return
        try:
            node_info = json.loads(body)
            conditions = node_info['status']['conditions']
            i = [c['type'] for c in conditions].index('NetworkUnavailable')
            if conditions[i]['status'] == 'True':
                hookenv.log('Clearing NetworkUnavailable from {}'.format(
                    node_name))
                conditions[i] = {
                    "type": "NetworkUnavailable",
                    "status": "False",
                    "reason": "RouteCreated",
                    "message": "Manually set through k8s api",
                }
                req = Request(url, method='PUT',
                              data=json.dumps(node_info).encode('utf8'),
                              headers={'Content-Type': 'application/json'})
                with urlopen(req) as response:
                    code = response.getcode()
                    body = response.read().decode('utf8')
                if code not in (200, 201, 202):
                    hookenv.log('failed to update node status [{}]: {}'.format(
                        code, body), hookenv.ERROR)
                    return
        except (json.JSONDecodeError, KeyError):
            hookenv.log('failed to parse node status: {}'.format(body),
                        hookenv.ERROR)
            return


def apiserverVersion():
    cmd = 'kube-apiserver --version'.split()
    version_string = check_output(cmd).decode('utf-8')
    return tuple(int(q) for q in re.findall("[0-9]+", version_string)[:3])


def touch(fname):
    try:
        os.utime(fname, None)
    except OSError:
        open(fname, 'a').close()


def getStorageBackend():
    storage_backend = hookenv.config('storage-backend')
    if storage_backend == 'auto':
        storage_backend = leader_get('auto_storage_backend')
    return storage_backend


@when('leadership.is_leader')
@when_not('leadership.set.cluster_tag')
def create_cluster_tag():
    cluster_tag = 'kubernetes-{}'.format(token_generator().lower())
    leader_set(cluster_tag=cluster_tag)


@when('leadership.set.cluster_tag',
      'kube-control.connected')
@when_not('kubernetes-master.cluster-tag-sent')
def send_cluster_tag():
    cluster_tag = leader_get('cluster_tag')
    kube_control = endpoint_from_flag('kube-control.connected')
    kube_control.set_cluster_tag(cluster_tag)
    set_state('kubernetes-master.cluster-tag-sent')


@when_not('kube-control.connected')
def clear_cluster_tag_sent():
    remove_state('kubernetes-master.cluster-tag-sent')


@when_any('endpoint.aws.joined',
          'endpoint.gcp.joined',
          'endpoint.openstack.joined',
          'endpoint.vsphere.joined',
          'endpoint.azure.joined')
@when_not('kubernetes-master.cloud.ready')
def set_cloud_pending():
    k8s_version = get_version('kube-apiserver')
    k8s_1_11 = k8s_version >= (1, 11)
    k8s_1_12 = k8s_version >= (1, 12)
    vsphere_joined = is_state('endpoint.vsphere.joined')
    azure_joined = is_state('endpoint.azure.joined')
    if (vsphere_joined and not k8s_1_12) or (azure_joined and not k8s_1_11):
        set_state('kubernetes-master.cloud.blocked')
    else:
        remove_state('kubernetes-master.cloud.blocked')
    set_state('kubernetes-master.cloud.pending')


@when_any('endpoint.aws.joined',
          'endpoint.gcp.joined',
          'endpoint.azure.joined')
@when('leadership.set.cluster_tag')
@when_not('kubernetes-master.cloud.request-sent')
def request_integration():
    hookenv.status_set('maintenance', 'requesting cloud integration')
    cluster_tag = leader_get('cluster_tag')
    if is_state('endpoint.aws.joined'):
        cloud = endpoint_from_flag('endpoint.aws.joined')
        cloud.tag_instance({
            'kubernetes.io/cluster/{}'.format(cluster_tag): 'owned',
            'k8s.io/role/master': 'true',
        })
        cloud.tag_instance_security_group({
            'kubernetes.io/cluster/{}'.format(cluster_tag): 'owned',
        })
        cloud.tag_instance_subnet({
            'kubernetes.io/cluster/{}'.format(cluster_tag): 'owned',
        })
        cloud.enable_object_storage_management(['kubernetes-*'])
        cloud.enable_load_balancer_management()
    elif is_state('endpoint.gcp.joined'):
        cloud = endpoint_from_flag('endpoint.gcp.joined')
        cloud.label_instance({
            'k8s-io-cluster-name': cluster_tag,
            'k8s-io-role-master': 'master',
        })
        cloud.enable_object_storage_management()
        cloud.enable_security_management()
    elif is_state('endpoint.azure.joined'):
        cloud = endpoint_from_flag('endpoint.azure.joined')
        cloud.tag_instance({
            'k8s-io-cluster-name': cluster_tag,
            'k8s-io-role-master': 'master',
        })
        cloud.enable_object_storage_management()
        cloud.enable_security_management()
    cloud.enable_instance_inspection()
    cloud.enable_network_management()
    cloud.enable_dns_management()
    cloud.enable_block_storage_management()
    set_state('kubernetes-master.cloud.request-sent')


@when_none('endpoint.aws.joined',
           'endpoint.gcp.joined',
           'endpoint.openstack.joined',
           'endpoint.vsphere.joined',
           'endpoint.azure.joined')
def clear_cloud_flags():
    remove_state('kubernetes-master.cloud.pending')
    remove_state('kubernetes-master.cloud.request-sent')
    remove_state('kubernetes-master.cloud.blocked')
    remove_state('kubernetes-master.cloud.ready')


@when_any('endpoint.aws.ready',
          'endpoint.gcp.ready',
          'endpoint.openstack.ready',
          'endpoint.vsphere.ready',
          'endpoint.azure.ready')
@when_not('kubernetes-master.cloud.blocked',
          'kubernetes-master.cloud.ready')
def cloud_ready():
    if is_state('endpoint.gcp.ready'):
        write_gcp_snap_config('kube-apiserver')
        write_gcp_snap_config('kube-controller-manager')
    elif is_state('endpoint.openstack.ready'):
        write_openstack_snap_config('kube-apiserver')
        write_openstack_snap_config('kube-controller-manager')
    elif is_state('endpoint.vsphere.ready'):
        _write_vsphere_snap_config('kube-apiserver')
        _write_vsphere_snap_config('kube-controller-manager')
    elif is_state('endpoint.azure.ready'):
        write_azure_snap_config('kube-apiserver')
        write_azure_snap_config('kube-controller-manager')
    remove_state('kubernetes-master.cloud.pending')
    set_state('kubernetes-master.cloud.ready')
    remove_state('kubernetes-master.components.started')  # force restart


@when('kubernetes-master.cloud.ready',
      'endpoint.openstack.ready.changed')
def update_openstack():
    remove_state('kubernetes-master.cloud.ready')
    remove_state('endpoint.openstack.ready.changed')


def _cdk_addons_template_path():
    return Path('/snap/cdk-addons/current/templates')


def _write_vsphere_snap_config(component):
    # vsphere requires additional cloud config
    vsphere = endpoint_from_flag('endpoint.vsphere.ready')

    # NB: vsphere provider will ask kube-apiserver and -controller-manager to
    # find a uuid from sysfs unless a global config value is set. Our strict
    # snaps cannot read sysfs, so let's do it in the charm. An invalid uuid is
    # not fatal for storage, but it will muddy the logs; try to get it right.
    uuid_file = '/sys/class/dmi/id/product_uuid'
    try:
        with open(uuid_file, 'r') as f:
            uuid = f.read().strip()
    except IOError as err:
        hookenv.log("Unable to read UUID from sysfs: {}".format(err))
        uuid = 'UNKNOWN'

    comp_cloud_config_path = cloud_config_path(component)
    comp_cloud_config_path.write_text('\n'.join([
        '[Global]',
        'insecure-flag = true',
        'datacenters = "{}"'.format(vsphere.datacenter),
        'vm-uuid = "VMware-{}"'.format(uuid),
        '[VirtualCenter "{}"]'.format(vsphere.vsphere_ip),
        'user = {}'.format(vsphere.user),
        'password = {}'.format(vsphere.password),
        '[Workspace]',
        'server = {}'.format(vsphere.vsphere_ip),
        'datacenter = "{}"'.format(vsphere.datacenter),
        'default-datastore = "{}"'.format(vsphere.datastore),
        'folder = "kubernetes"',
        'resourcepool-path = ""',
        '[Disk]',
        'scsicontrollertype = "pvscsi"',
    ]))


@when('config.changed.keystone-policy', 'keystone-credentials.available.auth')
def generate_keystone_configmap():
    os.makedirs(keystone_root, exist_ok=True)

    keystone_policy_path = keystone_root + '/keystone-policy.yaml'
    keystone_policy = hookenv.config('keystone-policy')
    if keystone_policy:
        write_file_with_autogenerated_header(keystone_policy_path,
                                             keystone_policy)
        kubectl_manifest('apply', keystone_policy_path)
    else:
        kubectl_manifest('delete', keystone_policy_path)
        remove_if_exists(keystone_policy_path)


@when('keystone-credentials.connected')
def setup_keystone_user():
    # This seems silly, but until we request a user from keystone
    # we don't get information about the keystone server...
    ks = endpoint_from_flag('keystone-credentials.connected')
    ks.request_credentials('k8s')


@when('keystone.credentials.configured',
      'leadership.set.keystone-cdk-addons-configured')
@when_not('keystone.apiserver.configured')
def keystone_kick_apiserver():
    # if we have run configure, but we haven't configured the api server
    # because the service wasn't up yet, we need to keep trying
    etcd = endpoint_from_flag('etcd.available')
    configure_apiserver(etcd.get_connection_string())


@when('keystone-credentials.available.auth', 'certificates.ca.available',
      'certificates.client.cert.available', 'authentication.setup',
      'etcd.available', 'leadership.set.keystone-cdk-addons-configured')
def keystone_config():
    # first, we have to have the service set up before we can render this stuff
    ks = endpoint_from_flag('keystone-credentials.available.auth')
    data = {
        'host': ks.auth_host(),
        'proto': ks.auth_protocol(),
        'port': ks.auth_port(),
        'version': ks.api_version()
    }
    if data_changed('keystone', data):
        remove_state('keystone.credentials.configured')

        # we basically just call the other things we need to update
        etcd = endpoint_from_flag('etcd.available')
        lb = endpoint_from_flag('loadbalancer.available')
        ca = endpoint_from_flag('certificates.ca.available')
        client = endpoint_from_flag('certificates.client.cert.available')

        configure_apiserver(etcd.get_connection_string())
        if lb:
            loadbalancer_kubeconfig(lb, ca, client)
        else:
            create_self_config(ca, client)
        generate_keystone_configmap()
        set_state('keystone.credentials.configured')
