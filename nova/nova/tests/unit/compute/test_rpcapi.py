# Copyright 2012, Red Hat, Inc.
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

"""
Unit Tests for nova.compute.rpcapi
"""

import contextlib

import mock
from oslo_config import cfg
from oslo_serialization import jsonutils

from nova.compute import rpcapi as compute_rpcapi
from nova import context
from nova.objects import block_device as objects_block_dev
from nova import test
from nova.tests.unit import fake_block_device
from nova.tests.unit import fake_flavor
from nova.tests.unit import fake_instance

CONF = cfg.CONF


class ComputeRpcAPITestCase(test.NoDBTestCase):

    def setUp(self):
        super(ComputeRpcAPITestCase, self).setUp()
        self.context = context.get_admin_context()
        self.fake_flavor_obj = fake_flavor.fake_flavor_obj(self.context)
        self.fake_flavor = jsonutils.to_primitive(self.fake_flavor_obj)
        instance_attr = {'host': 'fake_host',
                         'instance_type_id': self.fake_flavor_obj['id'],
                         'instance_type': self.fake_flavor_obj}
        self.fake_instance_obj = fake_instance.fake_instance_obj(self.context,
                                                   **instance_attr)
        self.fake_instance = jsonutils.to_primitive(self.fake_instance_obj)
        self.fake_volume_bdm = objects_block_dev.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                    {'source_type': 'volume', 'destination_type': 'volume',
                     'instance_uuid': self.fake_instance_obj.uuid,
                     'volume_id': 'fake-volume-id'}))

    def _test_compute_api(self, method, rpc_method,
                          expected_args=None, **kwargs):
        ctxt = context.RequestContext('fake_user', 'fake_project')

        rpcapi = kwargs.pop('rpcapi_class', compute_rpcapi.ComputeAPI)()
        self.assertIsNotNone(rpcapi.client)
        self.assertEqual(rpcapi.client.target.topic, CONF.compute_topic)

        orig_prepare = rpcapi.client.prepare
        base_version = rpcapi.client.target.version
        expected_version = kwargs.pop('version', base_version)

        expected_kwargs = kwargs.copy()
        if expected_args:
            expected_kwargs.update(expected_args)
        if 'host_param' in expected_kwargs:
            expected_kwargs['host'] = expected_kwargs.pop('host_param')
        else:
            expected_kwargs.pop('host', None)

        cast_and_call = ['confirm_resize', 'stop_instance']
        if rpc_method == 'call' and method in cast_and_call:
            if method == 'confirm_resize':
                kwargs['cast'] = False
            else:
                kwargs['do_cast'] = False
        if 'host' in kwargs:
            host = kwargs['host']
        elif 'instances' in kwargs:
            host = kwargs['instances'][0]['host']
        else:
            host = kwargs['instance']['host']

        if method == 'rebuild_instance' and 'node' in expected_kwargs:
            expected_kwargs['scheduled_node'] = expected_kwargs.pop('node')

        with contextlib.nested(
            mock.patch.object(rpcapi.client, rpc_method),
            mock.patch.object(rpcapi.client, 'prepare'),
            mock.patch.object(rpcapi.client, 'can_send_version'),
        ) as (
            rpc_mock, prepare_mock, csv_mock
        ):
            prepare_mock.return_value = rpcapi.client
            if '_return_value' in kwargs:
                rpc_mock.return_value = kwargs.pop('_return_value')
                del expected_kwargs['_return_value']
            elif 'return_bdm_object' in kwargs:
                del kwargs['return_bdm_object']
                rpc_mock.return_value = objects_block_dev.BlockDeviceMapping()
            elif rpc_method == 'call':
                rpc_mock.return_value = 'foo'
            else:
                rpc_mock.return_value = None
            csv_mock.side_effect = (
                lambda v: orig_prepare(version=v).can_send_version())

            retval = getattr(rpcapi, method)(ctxt, **kwargs)
            self.assertEqual(retval, rpc_mock.return_value)

            prepare_mock.assert_called_once_with(version=expected_version,
                                                 server=host)
            rpc_mock.assert_called_once_with(ctxt, method, **expected_kwargs)

    def test_add_aggregate_host(self):
        self._test_compute_api('add_aggregate_host', 'cast',
                aggregate={'id': 'fake_id'}, host_param='host', host='host',
                subordinate_info={})

    def test_add_fixed_ip_to_instance(self):
        self._test_compute_api('add_fixed_ip_to_instance', 'cast',
                instance=self.fake_instance_obj, network_id='id',
                version='4.0')

    def test_attach_interface(self):
        self._test_compute_api('attach_interface', 'call',
                instance=self.fake_instance_obj, network_id='id',
                port_id='id2', version='4.0', requested_ip='192.168.1.50')

    def test_attach_volume(self):
        self._test_compute_api('attach_volume', 'cast',
                instance=self.fake_instance_obj, bdm=self.fake_volume_bdm,
                version='4.0')

    def test_change_instance_metadata(self):
        self._test_compute_api('change_instance_metadata', 'cast',
                instance=self.fake_instance_obj, diff={}, version='4.0')

    def test_check_instance_shared_storage(self):
        self._test_compute_api('check_instance_shared_storage', 'call',
                instance=self.fake_instance_obj, data='foo',
                version='4.0')

    def test_confirm_resize_cast(self):
        self._test_compute_api('confirm_resize', 'cast',
                instance=self.fake_instance_obj, migration={'id': 'foo'},
                host='host', reservations=list('fake_res'))

    def test_confirm_resize_call(self):
        self._test_compute_api('confirm_resize', 'call',
                instance=self.fake_instance_obj, migration={'id': 'foo'},
                host='host', reservations=list('fake_res'))

    def test_detach_interface(self):
        self._test_compute_api('detach_interface', 'cast',
                version='4.0', instance=self.fake_instance_obj,
                port_id='fake_id')

    def test_detach_volume(self):
        self._test_compute_api('detach_volume', 'cast',
                instance=self.fake_instance_obj, volume_id='id',
                version='4.0')

    def test_finish_resize(self):
        self._test_compute_api('finish_resize', 'cast',
                instance=self.fake_instance_obj, migration={'id': 'foo'},
                image='image', disk_info='disk_info', host='host',
                reservations=list('fake_res'))

    def test_finish_revert_resize(self):
        self._test_compute_api('finish_revert_resize', 'cast',
                instance=self.fake_instance_obj, migration={'id': 'fake_id'},
                host='host', reservations=list('fake_res'))

    def test_get_console_output(self):
        self._test_compute_api('get_console_output', 'call',
                instance=self.fake_instance_obj, tail_length='tl',
                version='4.0')

    def test_get_console_pool_info(self):
        self._test_compute_api('get_console_pool_info', 'call',
                console_type='type', host='host')

    def test_get_console_topic(self):
        self._test_compute_api('get_console_topic', 'call', host='host')

    def test_get_diagnostics(self):
        self._test_compute_api('get_diagnostics', 'call',
                instance=self.fake_instance_obj, version='4.0')

    def test_get_instance_diagnostics(self):
        expected_args = {'instance': self.fake_instance}
        self._test_compute_api('get_instance_diagnostics', 'call',
                expected_args, instance=self.fake_instance_obj,
                version='4.0')

    def test_get_vnc_console(self):
        self._test_compute_api('get_vnc_console', 'call',
                instance=self.fake_instance_obj, console_type='type',
                version='4.0')

    def test_get_spice_console(self):
        self._test_compute_api('get_spice_console', 'call',
                instance=self.fake_instance_obj, console_type='type',
                version='4.0')

    def test_get_rdp_console(self):
        self._test_compute_api('get_rdp_console', 'call',
                instance=self.fake_instance_obj, console_type='type',
                version='4.0')

    def test_get_serial_console(self):
        self._test_compute_api('get_serial_console', 'call',
                instance=self.fake_instance_obj, console_type='serial',
                version='4.0')

    def test_get_mks_console(self):
        self._test_compute_api('get_mks_console', 'call',
                instance=self.fake_instance_obj, console_type='webmks',
                version='4.3')

    def test_validate_console_port(self):
        self._test_compute_api('validate_console_port', 'call',
                instance=self.fake_instance_obj, port="5900",
                console_type="novnc", version='4.0')

    def test_host_maintenance_mode(self):
        self._test_compute_api('host_maintenance_mode', 'call',
                host_param='param', mode='mode', host='host')

    def test_host_power_action(self):
        self._test_compute_api('host_power_action', 'call', action='action',
                host='host')

    def test_inject_network_info(self):
        self._test_compute_api('inject_network_info', 'cast',
                instance=self.fake_instance_obj)

    def test_live_migration(self):
        self._test_compute_api('live_migration', 'cast',
                instance=self.fake_instance_obj, dest='dest',
                block_migration='blockity_block', host='tsoh',
                migration='migration',
                migrate_data={}, version='4.2')

    def test_post_live_migration_at_destination(self):
        self._test_compute_api('post_live_migration_at_destination', 'cast',
                instance=self.fake_instance_obj,
                block_migration='block_migration', host='host', version='4.0')

    def test_pause_instance(self):
        self._test_compute_api('pause_instance', 'cast',
                               instance=self.fake_instance_obj)

    def test_soft_delete_instance(self):
        self._test_compute_api('soft_delete_instance', 'cast',
                instance=self.fake_instance_obj,
                reservations=['uuid1', 'uuid2'])

    def test_swap_volume(self):
        self._test_compute_api('swap_volume', 'cast',
                instance=self.fake_instance_obj, old_volume_id='oldid',
                new_volume_id='newid')

    def test_restore_instance(self):
        self._test_compute_api('restore_instance', 'cast',
                instance=self.fake_instance_obj, version='4.0')

    def test_pre_live_migration(self):
        self._test_compute_api('pre_live_migration', 'call',
                instance=self.fake_instance_obj,
                block_migration='block_migration', disk='disk', host='host',
                migrate_data=None, version='4.0')

    def test_prep_resize(self):
        self._test_compute_api('prep_resize', 'cast',
                instance=self.fake_instance_obj,
                instance_type=self.fake_flavor_obj,
                image='fake_image', host='host',
                reservations=list('fake_res'),
                request_spec='fake_spec',
                filter_properties={'fakeprop': 'fakeval'},
                node='node', clean_shutdown=True, version='4.1')
        self.flags(compute='4.0', group='upgrade_levels')
        expected_args = {'instance_type': self.fake_flavor}
        self._test_compute_api('prep_resize', 'cast', expected_args,
                instance=self.fake_instance_obj,
                instance_type=self.fake_flavor_obj,
                image='fake_image', host='host',
                reservations=list('fake_res'),
                request_spec='fake_spec',
                filter_properties={'fakeprop': 'fakeval'},
                node='node', clean_shutdown=True, version='4.0')

    def test_reboot_instance(self):
        self.maxDiff = None
        self._test_compute_api('reboot_instance', 'cast',
                instance=self.fake_instance_obj,
                block_device_info={},
                reboot_type='type')

    def test_rebuild_instance(self):
        self._test_compute_api('rebuild_instance', 'cast', new_pass='None',
                injected_files='None', image_ref='None', orig_image_ref='None',
                bdms=[], instance=self.fake_instance_obj, host='new_host',
                orig_sys_metadata=None, recreate=True, on_shared_storage=True,
                preserve_ephemeral=True, migration=None, node=None,
                limits=None, version='4.5')

    def test_rebuild_instance_downgrade(self):
        self.flags(group='upgrade_levels', compute='4.0')
        self._test_compute_api('rebuild_instance', 'cast', new_pass='None',
                injected_files='None', image_ref='None', orig_image_ref='None',
                bdms=[], instance=self.fake_instance_obj, host='new_host',
                orig_sys_metadata=None, recreate=True, on_shared_storage=True,
                preserve_ephemeral=True, version='4.0')

    def test_reserve_block_device_name(self):
        self._test_compute_api('reserve_block_device_name', 'call',
                instance=self.fake_instance_obj, device='device',
                volume_id='id', disk_bus='ide', device_type='cdrom',
                version='4.0',
                _return_value=objects_block_dev.BlockDeviceMapping())

    def refresh_provider_fw_rules(self):
        self._test_compute_api('refresh_provider_fw_rules', 'cast',
                host='host')

    def test_refresh_security_group_rules(self):
        self._test_compute_api('refresh_security_group_rules', 'cast',
                security_group_id='id', host='host', version='4.0')

    def test_refresh_security_group_members(self):
        self._test_compute_api('refresh_security_group_members', 'cast',
                security_group_id='id', host='host', version='4.0')

    def test_refresh_instance_security_rules(self):
        expected_args = {'instance': self.fake_instance_obj}
        self._test_compute_api('refresh_instance_security_rules', 'cast',
                expected_args, host='fake_host',
                instance=self.fake_instance_obj, version='4.4')

    def test_remove_aggregate_host(self):
        self._test_compute_api('remove_aggregate_host', 'cast',
                aggregate={'id': 'fake_id'}, host_param='host', host='host',
                subordinate_info={})

    def test_remove_fixed_ip_from_instance(self):
        self._test_compute_api('remove_fixed_ip_from_instance', 'cast',
                instance=self.fake_instance_obj, address='addr',
                version='4.0')

    def test_remove_volume_connection(self):
        self._test_compute_api('remove_volume_connection', 'call',
                instance=self.fake_instance_obj, volume_id='id', host='host',
                version='4.0')

    def test_rescue_instance(self):
        self._test_compute_api('rescue_instance', 'cast',
            instance=self.fake_instance_obj, rescue_password='pw',
            rescue_image_ref='fake_image_ref',
            clean_shutdown=True, version='4.0')

    def test_reset_network(self):
        self._test_compute_api('reset_network', 'cast',
                instance=self.fake_instance_obj)

    def test_resize_instance(self):
        self._test_compute_api('resize_instance', 'cast',
                instance=self.fake_instance_obj, migration={'id': 'fake_id'},
                image='image', instance_type=self.fake_flavor_obj,
                reservations=list('fake_res'),
                clean_shutdown=True, version='4.1')
        self.flags(compute='4.0', group='upgrade_levels')
        expected_args = {'instance_type': self.fake_flavor}
        self._test_compute_api('resize_instance', 'cast', expected_args,
                instance=self.fake_instance_obj, migration={'id': 'fake_id'},
                image='image', instance_type=self.fake_flavor_obj,
                reservations=list('fake_res'),
                clean_shutdown=True, version='4.0')

    def test_resume_instance(self):
        self._test_compute_api('resume_instance', 'cast',
                               instance=self.fake_instance_obj)

    def test_revert_resize(self):
        self._test_compute_api('revert_resize', 'cast',
                instance=self.fake_instance_obj, migration={'id': 'fake_id'},
                host='host', reservations=list('fake_res'))

    def test_set_admin_password(self):
        self._test_compute_api('set_admin_password', 'call',
                instance=self.fake_instance_obj, new_pass='pw',
                version='4.0')

    def test_set_host_enabled(self):
        self._test_compute_api('set_host_enabled', 'call',
                enabled='enabled', host='host')

    def test_get_host_uptime(self):
        self._test_compute_api('get_host_uptime', 'call', host='host')

    def test_backup_instance(self):
        self._test_compute_api('backup_instance', 'cast',
                instance=self.fake_instance_obj, image_id='id',
                backup_type='type', rotation='rotation')

    def test_snapshot_instance(self):
        self._test_compute_api('snapshot_instance', 'cast',
                instance=self.fake_instance_obj, image_id='id')

    def test_start_instance(self):
        self._test_compute_api('start_instance', 'cast',
                instance=self.fake_instance_obj)

    def test_stop_instance_cast(self):
        self._test_compute_api('stop_instance', 'cast',
                instance=self.fake_instance_obj,
                clean_shutdown=True, version='4.0')

    def test_stop_instance_call(self):
        self._test_compute_api('stop_instance', 'call',
                instance=self.fake_instance_obj,
                clean_shutdown=True, version='4.0')

    def test_suspend_instance(self):
        self._test_compute_api('suspend_instance', 'cast',
                               instance=self.fake_instance_obj)

    def test_terminate_instance(self):
        self._test_compute_api('terminate_instance', 'cast',
                instance=self.fake_instance_obj, bdms=[],
                reservations=['uuid1', 'uuid2'], version='4.0')

    def test_unpause_instance(self):
        self._test_compute_api('unpause_instance', 'cast',
                               instance=self.fake_instance_obj)

    def test_unrescue_instance(self):
        self._test_compute_api('unrescue_instance', 'cast',
                instance=self.fake_instance_obj, version='4.0')

    def test_shelve_instance(self):
        self._test_compute_api('shelve_instance', 'cast',
                instance=self.fake_instance_obj, image_id='image_id',
                clean_shutdown=True, version='4.0')

    def test_shelve_offload_instance(self):
        self._test_compute_api('shelve_offload_instance', 'cast',
                instance=self.fake_instance_obj,
                clean_shutdown=True, version='4.0')

    def test_unshelve_instance(self):
        self._test_compute_api('unshelve_instance', 'cast',
                instance=self.fake_instance_obj, host='host', image='image',
                filter_properties={'fakeprop': 'fakeval'}, node='node',
                version='4.0')

    def test_volume_snapshot_create(self):
        self._test_compute_api('volume_snapshot_create', 'cast',
                instance=self.fake_instance_obj, volume_id='fake_id',
                create_info={}, version='4.0')

    def test_volume_snapshot_delete(self):
        self._test_compute_api('volume_snapshot_delete', 'cast',
                instance=self.fake_instance_obj, volume_id='fake_id',
                snapshot_id='fake_id2', delete_info={}, version='4.0')

    def test_external_instance_event(self):
        self._test_compute_api('external_instance_event', 'cast',
                               instances=[self.fake_instance_obj],
                               events=['event'],
                               version='4.0')

    def test_build_and_run_instance(self):
        self._test_compute_api('build_and_run_instance', 'cast',
                instance=self.fake_instance_obj, host='host', image='image',
                request_spec={'request': 'spec'}, filter_properties=[],
                admin_password='passwd', injected_files=None,
                requested_networks=['network1'], security_groups=None,
                block_device_mapping=None, node='node', limits=[],
                version='4.0')

    def test_quiesce_instance(self):
        self._test_compute_api('quiesce_instance', 'call',
                instance=self.fake_instance_obj, version='4.0')

    def test_unquiesce_instance(self):
        self._test_compute_api('unquiesce_instance', 'cast',
                instance=self.fake_instance_obj, mapping=None, version='4.0')
