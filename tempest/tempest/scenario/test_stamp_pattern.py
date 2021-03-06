# Copyright 2013 NEC Corporation
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

import time

from oslo_log import log as logging
from tempest_lib import decorators
from tempest_lib import exceptions as lib_exc
import testtools

from tempest.common.utils import data_utils
from tempest import config
from tempest import exceptions
from tempest.scenario import manager
from tempest import test
import tempest.test

CONF = config.CONF

LOG = logging.getLogger(__name__)


class TestStampPattern(manager.ScenarioTest):
    """The test suite for both snapshoting and attaching of volume

    This test is for snapshotting an instance/volume and attaching the volume
    created from snapshot to the instance booted from snapshot.
    The following is the scenario outline:
    1. Boot an instance "instance1"
    2. Create a volume "volume1"
    3. Attach volume1 to instance1
    4. Create a filesystem on volume1
    5. Mount volume1
    6. Create a file which timestamp is written in volume1
    7. Unmount volume1
    8. Detach volume1 from instance1
    9. Get a snapshot "snapshot_from_volume" of volume1
    10. Get a snapshot "snapshot_from_instance" of instance1
    11. Boot an instance "instance2" from snapshot_from_instance
    12. Create a volume "volume2"  from snapshot_from_volume
    13. Attach volume2 to instance2
    14. Check the existence of a file which created at 6. in volume2
    """

    @classmethod
    def skip_checks(cls):
        super(TestStampPattern, cls).skip_checks()
        if not CONF.volume_feature_enabled.snapshot:
            raise cls.skipException("Cinder volume snapshots are disabled")

    def _wait_for_volume_snapshot_status(self, volume_snapshot, status):
        self.snapshots_client.wait_for_snapshot_status(volume_snapshot['id'],
                                                       status)

    def _create_volume_snapshot(self, volume):
        snapshot_name = data_utils.rand_name('scenario-snapshot')
        snapshot = self.snapshots_client.create_snapshot(
            volume_id=volume['id'], display_name=snapshot_name)['snapshot']

        def cleaner():
            self.snapshots_client.delete_snapshot(snapshot['id'])
            try:
                while self.snapshots_client.show_snapshot(
                    snapshot['id'])['snapshot']:
                    time.sleep(1)
            except lib_exc.NotFound:
                pass
        self.addCleanup(cleaner)
        self._wait_for_volume_status(volume, 'available')
        self.snapshots_client.wait_for_snapshot_status(snapshot['id'],
                                                       'available')
        self.assertEqual(snapshot_name, snapshot['display_name'])
        return snapshot

    def _wait_for_volume_status(self, volume, status):
        self.volumes_client.wait_for_volume_status(volume['id'], status)

    def _create_volume(self, snapshot_id=None):
        return self.create_volume(snapshot_id=snapshot_id)

    def _attach_volume(self, server, volume):
        attached_volume = self.servers_client.attach_volume(
            server['id'], volumeId=volume['id'], device='/dev/%s'
            % CONF.compute.volume_device_name)['volumeAttachment']
        self.assertEqual(volume['id'], attached_volume['id'])
        self._wait_for_volume_status(attached_volume, 'in-use')

    def _detach_volume(self, server, volume):
        self.servers_client.detach_volume(server['id'], volume['id'])
        self._wait_for_volume_status(volume, 'available')

    def _wait_for_volume_available_on_the_system(self, server_or_ip,
                                                 private_key):
        ssh = self.get_remote_client(server_or_ip, private_key=private_key)

        def _func():
            part = ssh.get_partitions()
            LOG.debug("Partitions:%s" % part)
            return CONF.compute.volume_device_name in part

        if not tempest.test.call_until_true(_func,
                                            CONF.compute.build_timeout,
                                            CONF.compute.build_interval):
            raise exceptions.TimeoutException

    @decorators.skip_because(bug="1205344")
    @test.idempotent_id('10fd234a-515c-41e5-b092-8323060598c5')
    @testtools.skipUnless(CONF.compute_feature_enabled.snapshot,
                          'Snapshotting is not available.')
    @tempest.test.services('compute', 'network', 'volume', 'image')
    def test_stamp_pattern(self):
        # prepare for booting an instance
        keypair = self.create_keypair()
        security_group = self._create_security_group()

        # boot an instance and create a timestamp file in it
        volume = self._create_volume()
        server = self.create_server(
            image_id=CONF.compute.image_ref,
            key_name=keypair['name'],
            security_groups=security_group,
            wait_until='ACTIVE')

        # create and add floating IP to server1
        ip_for_server = self.get_server_or_ip(server)

        self._attach_volume(server, volume)
        self._wait_for_volume_available_on_the_system(ip_for_server,
                                                      keypair['private_key'])
        timestamp = self.create_timestamp(ip_for_server,
                                          CONF.compute.volume_device_name,
                                          private_key=keypair['private_key'])
        self._detach_volume(server, volume)

        # snapshot the volume
        volume_snapshot = self._create_volume_snapshot(volume)

        # snapshot the instance
        snapshot_image = self.create_server_snapshot(server=server)

        # create second volume from the snapshot(volume2)
        volume_from_snapshot = self._create_volume(
            snapshot_id=volume_snapshot['id'])

        # boot second instance from the snapshot(instance2)
        server_from_snapshot = self.create_server(
            image_id=snapshot_image['id'],
            key_name=keypair['name'],
            security_groups=security_group)

        # create and add floating IP to server_from_snapshot
        ip_for_snapshot = self.get_server_or_ip(server_from_snapshot)

        # attach volume2 to instance2
        self._attach_volume(server_from_snapshot, volume_from_snapshot)
        self._wait_for_volume_available_on_the_system(ip_for_snapshot,
                                                      keypair['private_key'])

        # check the existence of the timestamp file in the volume2
        timestamp2 = self.get_timestamp(ip_for_snapshot,
                                        CONF.compute.volume_device_name,
                                        private_key=keypair['private_key'])
        self.assertEqual(timestamp, timestamp2)
