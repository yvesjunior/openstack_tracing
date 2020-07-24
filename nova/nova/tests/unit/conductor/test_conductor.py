#    Copyright 2012 IBM Corp.
#    Copyright 2013 Red Hat, Inc.
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

"""Tests for the conductor service."""

import contextlib
import copy
import uuid

import mock
from mox3 import mox
import oslo_messaging as messaging
from oslo_utils import timeutils
import six

from nova.api.ec2 import ec2utils
from nova.compute import arch
from nova.compute import flavors
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova import conductor
from nova.conductor import api as conductor_api
from nova.conductor import manager as conductor_manager
from nova.conductor import rpcapi as conductor_rpcapi
from nova.conductor.tasks import live_migrate
from nova.conductor.tasks import migrate
from nova import context
from nova import db
from nova.db.sqlalchemy import models
from nova import exception as exc
from nova.image import api as image_api
from nova import notifications
from nova import objects
from nova.objects import base as obj_base
from nova.objects import block_device as block_device_obj
from nova.objects import fields
from nova import rpc
from nova.scheduler import client as scheduler_client
from nova.scheduler import utils as scheduler_utils
from nova import test
from nova.tests.unit import cast_as_call
from nova.tests.unit.compute import test_compute
from nova.tests.unit import fake_block_device
from nova.tests.unit import fake_instance
from nova.tests.unit import fake_notifier
from nova.tests.unit import fake_server_actions
from nova.tests.unit import fake_utils
from nova.tests.unit.objects import test_volume_usage
from nova import utils


FAKE_IMAGE_REF = 'fake-image-ref'


class FakeContext(context.RequestContext):
    def elevated(self):
        """Return a consistent elevated context so we can detect it."""
        if not hasattr(self, '_elevated'):
            self._elevated = super(FakeContext, self).elevated()
        return self._elevated


class _BaseTestCase(object):
    def setUp(self):
        super(_BaseTestCase, self).setUp()
        self.db = None
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = FakeContext(self.user_id, self.project_id)

        fake_notifier.stub_notifier(self.stubs)
        self.addCleanup(fake_notifier.reset)

        def fake_deserialize_context(serializer, ctxt_dict):
            self.assertEqual(self.context.user_id, ctxt_dict['user_id'])
            self.assertEqual(self.context.project_id, ctxt_dict['project_id'])
            return self.context

        self.stubs.Set(rpc.RequestContextSerializer, 'deserialize_context',
                       fake_deserialize_context)

        fake_utils.stub_out_utils_spawn_n(self.stubs)

    def test_provider_fw_rule_get_all(self):
        fake_rules = ['a', 'b', 'c']
        self.mox.StubOutWithMock(db, 'provider_fw_rule_get_all')
        db.provider_fw_rule_get_all(self.context).AndReturn(fake_rules)
        self.mox.ReplayAll()
        result = self.conductor.provider_fw_rule_get_all(self.context)
        self.assertEqual(result, fake_rules)


class ConductorTestCase(_BaseTestCase, test.TestCase):
    """Conductor Manager Tests."""
    def setUp(self):
        super(ConductorTestCase, self).setUp()
        self.conductor = conductor_manager.ConductorManager()
        self.conductor_manager = self.conductor

    def _create_fake_instance(self, params=None, type_name='m1.tiny'):
        if not params:
            params = {}

        inst = {}
        inst['vm_state'] = vm_states.ACTIVE
        inst['image_ref'] = FAKE_IMAGE_REF
        inst['reservation_id'] = 'r-fakeres'
        inst['user_id'] = self.user_id
        inst['project_id'] = self.project_id
        inst['host'] = 'fake_host'
        type_id = flavors.get_flavor_by_name(type_name)['id']
        inst['instance_type_id'] = type_id
        inst['ami_launch_index'] = 0
        inst['memory_mb'] = 0
        inst['vcpus'] = 0
        inst['root_gb'] = 0
        inst['ephemeral_gb'] = 0
        inst['architecture'] = arch.X86_64
        inst['os_type'] = 'Linux'
        inst['availability_zone'] = 'fake-az'
        inst.update(params)
        return db.instance_create(self.context, inst)

    def _do_update(self, instance_uuid, **updates):
        return self.conductor.instance_update(self.context, instance_uuid,
                                              updates, None)

    def test_instance_update(self):
        instance = self._create_fake_instance()
        new_inst = self._do_update(instance['uuid'],
                                   vm_state=vm_states.STOPPED)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.STOPPED)
        self.assertEqual(new_inst['vm_state'], instance['vm_state'])

    def test_instance_update_invalid_key(self):
        # NOTE(danms): the real DB API call ignores invalid keys
        if self.db is None:
            self.conductor = utils.ExceptionHelper(self.conductor)
            self.assertRaises(KeyError,
                              self._do_update, 'any-uuid', foobar=1)

    def test_instance_get_by_uuid(self):
        orig_instance = self._create_fake_instance()
        copy_instance = self.conductor.instance_get_by_uuid(
            self.context, orig_instance['uuid'], None)
        self.assertEqual(orig_instance['name'],
                         copy_instance['name'])

    def test_block_device_mapping_update_or_create(self):
        fake_bdm = {'id': 1, 'device_name': 'foo',
                    'source_type': 'volume', 'volume_id': 'fake-vol-id',
                    'destination_type': 'volume'}
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict(fake_bdm)
        fake_bdm2 = {'id': 1, 'device_name': 'foo2',
                     'source_type': 'volume', 'volume_id': 'fake-vol-id',
                     'destination_type': 'volume'}
        fake_bdm2 = fake_block_device.FakeDbBlockDeviceDict(fake_bdm2)
        cells_rpcapi = self.conductor.cells_rpcapi
        self.mox.StubOutWithMock(db, 'block_device_mapping_create')
        self.mox.StubOutWithMock(db, 'block_device_mapping_update')
        self.mox.StubOutWithMock(db, 'block_device_mapping_update_or_create')
        self.mox.StubOutWithMock(cells_rpcapi,
                                 'bdm_update_or_create_at_top')
        db.block_device_mapping_create(self.context,
                                       fake_bdm).AndReturn(fake_bdm2)
        cells_rpcapi.bdm_update_or_create_at_top(
                self.context, mox.IsA(block_device_obj.BlockDeviceMapping),
                create=True)
        db.block_device_mapping_update(self.context, fake_bdm['id'],
                                       fake_bdm).AndReturn(fake_bdm2)
        cells_rpcapi.bdm_update_or_create_at_top(
                self.context, mox.IsA(block_device_obj.BlockDeviceMapping),
                create=False)
        self.mox.ReplayAll()
        self.conductor.block_device_mapping_update_or_create(self.context,
                                                             fake_bdm,
                                                             create=True)
        self.conductor.block_device_mapping_update_or_create(self.context,
                                                             fake_bdm,
                                                             create=False)

    def test_instance_get_all_by_filters(self):
        filters = {'foo': 'bar'}
        self.mox.StubOutWithMock(db, 'instance_get_all_by_filters')
        db.instance_get_all_by_filters(self.context, filters,
                                       'fake-key', 'fake-sort',
                                       columns_to_join=None, use_subordinate=False)
        self.mox.ReplayAll()
        self.conductor.instance_get_all_by_filters(self.context, filters,
                                                   'fake-key', 'fake-sort',
                                                   None, False)

    def test_instance_get_all_by_filters_use_subordinate(self):
        filters = {'foo': 'bar'}
        self.mox.StubOutWithMock(db, 'instance_get_all_by_filters')
        db.instance_get_all_by_filters(self.context, filters,
                                       'fake-key', 'fake-sort',
                                       columns_to_join=None, use_subordinate=True)
        self.mox.ReplayAll()
        self.conductor.instance_get_all_by_filters(self.context, filters,
                                                   'fake-key', 'fake-sort',
                                                   columns_to_join=None,
                                                   use_subordinate=True)

    def test_instance_get_all_by_host(self):
        self.mox.StubOutWithMock(db, 'instance_get_all_by_host')
        self.mox.StubOutWithMock(db, 'instance_get_all_by_host_and_node')
        db.instance_get_all_by_host(self.context.elevated(),
                                    'host', None).AndReturn('result')
        db.instance_get_all_by_host_and_node(self.context.elevated(), 'host',
                                             'node').AndReturn('result')
        self.mox.ReplayAll()
        result = self.conductor.instance_get_all_by_host(self.context, 'host',
                None, None)
        self.assertEqual(result, 'result')
        result = self.conductor.instance_get_all_by_host(self.context, 'host',
                                                         'node', None)
        self.assertEqual(result, 'result')

    def _test_stubbed(self, name, dbargs, condargs,
                      db_result_listified=False, db_exception=None):

        self.mox.StubOutWithMock(db, name)
        if db_exception:
            getattr(db, name)(self.context, *dbargs).AndRaise(db_exception)
            getattr(db, name)(self.context, *dbargs).AndRaise(db_exception)
        else:
            getattr(db, name)(self.context, *dbargs).AndReturn(condargs)
            if name == 'service_get_by_compute_host':
                self.mox.StubOutWithMock(
                    objects.ComputeNodeList, 'get_all_by_host')
                objects.ComputeNodeList.get_all_by_host(
                    self.context, mox.IgnoreArg()
                ).AndReturn(['fake-compute'])
        self.mox.ReplayAll()
        if db_exception:
            self.assertRaises(messaging.ExpectedException,
                              self.conductor.service_get_all_by,
                              self.context, **condargs)

            self.conductor = utils.ExceptionHelper(self.conductor)

            self.assertRaises(db_exception.__class__,
                              self.conductor.service_get_all_by,
                              self.context, **condargs)
        else:
            result = self.conductor.service_get_all_by(self.context,
                                                       **condargs)
            if db_result_listified:
                if name == 'service_get_by_compute_host':
                    condargs['compute_node'] = ['fake-compute']
                self.assertEqual([condargs], result)
            else:
                self.assertEqual(condargs, result)

    def test_service_get_all(self):
        self._test_stubbed('service_get_all', (),
                dict(host=None, topic=None, binary=None))

    def test_service_get_by_host_and_topic(self):
        self._test_stubbed('service_get_by_host_and_topic',
                           ('host', 'topic'),
                           dict(topic='topic', host='host', binary=None))

    def test_service_get_all_by_topic(self):
        self._test_stubbed('service_get_all_by_topic',
                           ('topic',),
                           dict(topic='topic', host=None, binary=None))

    def test_service_get_all_by_host(self):
        self._test_stubbed('service_get_all_by_host',
                           ('host',),
                           dict(host='host', topic=None, binary=None))

    def test_service_get_by_compute_host(self):
        self._test_stubbed('service_get_by_compute_host',
                           ('host',),
                           dict(topic='compute', host='host', binary=None),
                           db_result_listified=True)

    def test_service_get_by_host_and_binary(self):
        self._test_stubbed('service_get_by_host_and_binary',
                           ('host', 'binary'),
                           dict(host='host', binary='binary', topic=None))

    def test_service_get_by_compute_host_not_found(self):
        self._test_stubbed('service_get_by_compute_host',
                           ('host',),
                           dict(topic='compute', host='host', binary=None),
                           db_exception=exc.ComputeHostNotFound(host='host'))

    def test_service_get_by_host_and_binary_not_found(self):
        self._test_stubbed('service_get_by_host_and_binary',
                           ('host', 'binary'),
                           dict(host='host', binary='binary', topic=None),
                           db_exception=exc.HostBinaryNotFound(binary='binary',
                                                               host='host'))

    def test_security_groups_trigger_handler(self):
        self.mox.StubOutWithMock(self.conductor_manager.security_group_api,
                                 'trigger_handler')
        self.conductor_manager.security_group_api.trigger_handler('event',
                                                                  self.context,
                                                                  'args')
        self.mox.ReplayAll()
        self.conductor.security_groups_trigger_handler(self.context,
                                                       'event', ['args'])

    def _test_object_action(self, is_classmethod, raise_exception):
        class TestObject(obj_base.NovaObject):
            def foo(self, raise_exception=False):
                if raise_exception:
                    raise Exception('test')
                else:
                    return 'test'

            @classmethod
            def bar(cls, context, raise_exception=False):
                if raise_exception:
                    raise Exception('test')
                else:
                    return 'test'

        obj_base.NovaObjectRegistry.register(TestObject)

        obj = TestObject()
        # NOTE(danms): After a trip over RPC, any tuple will be a list,
        # so use a list here to make sure we can handle it
        fake_args = []
        if is_classmethod:
            result = self.conductor.object_class_action(
                self.context, TestObject.obj_name(), 'bar', '1.0',
                fake_args, {'raise_exception': raise_exception})
        else:
            updates, result = self.conductor.object_action(
                self.context, obj, 'foo', fake_args,
                {'raise_exception': raise_exception})
        self.assertEqual('test', result)

    def test_object_action(self):
        self._test_object_action(False, False)

    def test_object_action_on_raise(self):
        self.assertRaises(messaging.ExpectedException,
                          self._test_object_action, False, True)

    def test_object_class_action(self):
        self._test_object_action(True, False)

    def test_object_class_action_on_raise(self):
        self.assertRaises(messaging.ExpectedException,
                          self._test_object_action, True, True)

    def test_object_action_copies_object(self):
        class TestObject(obj_base.NovaObject):
            fields = {'dict': fields.DictOfStringsField()}

            def touch_dict(self):
                self.dict['foo'] = 'bar'
                self.obj_reset_changes()

        obj_base.NovaObjectRegistry.register(TestObject)

        obj = TestObject()
        obj.dict = {}
        obj.obj_reset_changes()
        updates, result = self.conductor.object_action(
            self.context, obj, 'touch_dict', tuple(), {})
        # NOTE(danms): If conductor did not properly copy the object, then
        # the new and reference copies of the nested dict object will be
        # the same, and thus 'dict' will not be reported as changed
        self.assertIn('dict', updates)
        self.assertEqual({'foo': 'bar'}, updates['dict'])

    def test_object_class_action_versions(self):
        @obj_base.NovaObjectRegistry.register
        class TestObject(obj_base.NovaObject):
            VERSION = '1.10'

            @classmethod
            def foo(cls, context):
                return cls()

        versions = {
            'TestObject': '1.2',
            'OtherObj': '1.0',
        }
        with mock.patch.object(self.conductor_manager,
                               '_object_dispatch') as m:
            m.return_value = TestObject()
            m.return_value.obj_to_primitive = mock.MagicMock()
            self.conductor.object_class_action_versions(
                self.context, TestObject.obj_name(), 'foo', versions,
                tuple(), {})
            m.return_value.obj_to_primitive.assert_called_once_with(
                target_version='1.2', version_manifest=versions)

    def _test_expected_exceptions(self, db_method, conductor_method, errors,
                                  *args, **kwargs):
        # Tests that expected exceptions are handled properly.
        for error in errors:
            with mock.patch.object(db, db_method, side_effect=error):
                self.assertRaises(messaging.ExpectedException,
                                  conductor_method,
                                  self.context, *args, **kwargs)

    def test_action_event_start_expected_exceptions(self):
        error = exc.InstanceActionNotFound(request_id='1', instance_uuid='2')
        self._test_expected_exceptions(
            'action_event_start', self.conductor.action_event_start, [error],
            {'foo': 'bar'})

    def test_action_event_finish_expected_exceptions(self):
        errors = (exc.InstanceActionNotFound(request_id='1',
                                             instance_uuid='2'),
                  exc.InstanceActionEventNotFound(event='1', action_id='2'))
        self._test_expected_exceptions(
            'action_event_finish', self.conductor.action_event_finish,
            errors, {'foo': 'bar'})

    def test_instance_update_expected_exceptions(self):
        errors = (exc.InvalidUUID(uuid='foo'),
                  exc.InstanceNotFound(instance_id=1),
                  exc.UnexpectedTaskStateError(instance_uuid='fake_uuid',
                                               expected={'task_state': 'foo'},
                                               actual={'task_state': 'bar'}))
        self._test_expected_exceptions(
            'instance_update', self.conductor.instance_update,
            errors, None, {'foo': 'bar'}, None)

    def test_instance_get_by_uuid_expected_exceptions(self):
        error = exc.InstanceNotFound(instance_id=1)
        self._test_expected_exceptions(
            'instance_get_by_uuid', self.conductor.instance_get_by_uuid,
            [error], None, [])

    def test_aggregate_host_add_expected_exceptions(self):
        error = exc.AggregateHostExists(aggregate_id=1, host='foo')
        self._test_expected_exceptions(
            'aggregate_host_add', self.conductor.aggregate_host_add,
            [error], {'id': 1}, None)

    def test_aggregate_host_delete_expected_exceptions(self):
        error = exc.AggregateHostNotFound(aggregate_id=1, host='foo')
        self._test_expected_exceptions(
            'aggregate_host_delete', self.conductor.aggregate_host_delete,
            [error], {'id': 1}, None)

    def test_service_update_expected_exceptions(self):
        error = exc.ServiceNotFound(service_id=1)
        self._test_expected_exceptions(
            'service_update',
            self.conductor.service_update,
            [error], {'id': 1}, None)

    def test_service_destroy_expected_exceptions(self):
        error = exc.ServiceNotFound(service_id=1)
        self._test_expected_exceptions(
            'service_destroy',
            self.conductor.service_destroy,
            [error], 1)

    def _setup_aggregate_with_host(self):
        aggregate_ref = db.aggregate_create(self.context.elevated(),
                {'name': 'foo'}, metadata={'availability_zone': 'foo'})

        self.conductor.aggregate_host_add(self.context, aggregate_ref, 'bar')

        aggregate_ref = db.aggregate_get(self.context.elevated(),
                                         aggregate_ref['id'])

        return aggregate_ref

    def test_aggregate_host_add(self):
        aggregate_ref = self._setup_aggregate_with_host()

        self.assertIn('bar', aggregate_ref['hosts'])

        db.aggregate_delete(self.context.elevated(), aggregate_ref['id'])

    def test_aggregate_host_delete(self):
        aggregate_ref = self._setup_aggregate_with_host()

        self.conductor.aggregate_host_delete(self.context, aggregate_ref,
                'bar')

        aggregate_ref = db.aggregate_get(self.context.elevated(),
                aggregate_ref['id'])

        self.assertNotIn('bar', aggregate_ref['hosts'])

        db.aggregate_delete(self.context.elevated(), aggregate_ref['id'])

    def test_network_migrate_instance_start(self):
        self.mox.StubOutWithMock(self.conductor_manager.network_api,
                                 'migrate_instance_start')
        self.conductor_manager.network_api.migrate_instance_start(self.context,
                                                                  'instance',
                                                                  'migration')
        self.mox.ReplayAll()
        self.conductor.network_migrate_instance_start(self.context,
                                                      'instance',
                                                      'migration')

    def test_network_migrate_instance_finish(self):
        self.mox.StubOutWithMock(self.conductor_manager.network_api,
                                 'migrate_instance_finish')
        self.conductor_manager.network_api.migrate_instance_finish(
            self.context, 'instance', 'migration')
        self.mox.ReplayAll()
        self.conductor.network_migrate_instance_finish(self.context,
                                                       'instance',
                                                       'migration')

    def test_instance_destroy(self):
        instance = objects.Instance(id=1, uuid='fake-uuid')

        @mock.patch.object(instance, 'destroy')
        @mock.patch.object(obj_base, 'obj_to_primitive',
                           return_value='fake-result')
        def do_test(mock_to_primitive, mock_destroy):
            result = self.conductor.instance_destroy(self.context, instance)
            mock_destroy.assert_called_once_with()
            mock_to_primitive.assert_called_once_with(instance)
            self.assertEqual(result, 'fake-result')
        do_test()

    def test_compute_unrescue(self):
        self.mox.StubOutWithMock(self.conductor_manager.compute_api,
                                 'unrescue')
        self.conductor_manager.compute_api.unrescue(self.context, 'instance')
        self.mox.ReplayAll()
        self.conductor.compute_unrescue(self.context, 'instance')

    def test_instance_get_active_by_window_joined(self):
        self.mox.StubOutWithMock(db, 'instance_get_active_by_window_joined')
        db.instance_get_active_by_window_joined(self.context, 'fake-begin',
                                                'fake-end', 'fake-proj',
                                                'fake-host')
        self.mox.ReplayAll()
        self.conductor.instance_get_active_by_window_joined(
            self.context, 'fake-begin', 'fake-end', 'fake-proj', 'fake-host')

    def test_instance_fault_create(self):
        self.mox.StubOutWithMock(db, 'instance_fault_create')
        db.instance_fault_create(self.context, 'fake-values').AndReturn(
            'fake-result')
        self.mox.ReplayAll()
        result = self.conductor.instance_fault_create(self.context,
                                                      'fake-values')
        self.assertEqual(result, 'fake-result')

    def test_action_event_start(self):
        self.mox.StubOutWithMock(db, 'action_event_start')
        db.action_event_start(self.context, mox.IgnoreArg())
        self.mox.ReplayAll()
        self.conductor.action_event_start(self.context, {})

    def test_action_event_finish(self):
        self.mox.StubOutWithMock(db, 'action_event_finish')
        db.action_event_finish(self.context, mox.IgnoreArg())
        self.mox.ReplayAll()
        self.conductor.action_event_finish(self.context, {})

    def test_agent_build_get_by_triple(self):
        self.mox.StubOutWithMock(db, 'agent_build_get_by_triple')
        db.agent_build_get_by_triple(self.context, 'fake-hv', 'fake-os',
                                     'fake-arch').AndReturn('it worked')
        self.mox.ReplayAll()
        result = self.conductor.agent_build_get_by_triple(self.context,
                                                          'fake-hv',
                                                          'fake-os',
                                                          'fake-arch')
        self.assertEqual(result, 'it worked')

    def test_bw_usage_update(self):
        self.mox.StubOutWithMock(db, 'bw_usage_update')
        self.mox.StubOutWithMock(db, 'bw_usage_get')

        update_args = (self.context, 'uuid', 'mac', 0, 10, 20, 5, 10, 20)
        get_args = (self.context, 'uuid', 0, 'mac')

        db.bw_usage_update(*update_args, update_cells=True)
        db.bw_usage_get(*get_args).AndReturn('foo')

        self.mox.ReplayAll()
        result = self.conductor.bw_usage_update(*update_args,
                update_cells=True)
        self.assertEqual(result, 'foo')

    @mock.patch.object(notifications, 'audit_period_bounds')
    @mock.patch.object(notifications, 'bandwidth_usage')
    @mock.patch.object(compute_utils, 'notify_about_instance_usage')
    def test_notify_usage_exists(self, mock_notify, mock_bw, mock_audit):
        info = {
            'audit_period_beginning': 'start',
            'audit_period_ending': 'end',
            'bandwidth': 'bw_usage',
            'image_meta': {},
            'extra': 'info',
            }
        instance = objects.Instance(id=1, system_metadata={})

        mock_audit.return_value = ('start', 'end')
        mock_bw.return_value = 'bw_usage'

        self.conductor.notify_usage_exists(self.context, instance, False, True,
                                           system_metadata={},
                                           extra_usage_info=dict(extra='info'))

        class MatchInstance(object):
            def __eq__(self, thing):
                return thing.id == instance.id

        notifier = self.conductor_manager.notifier
        mock_audit.assert_called_once_with(False)
        mock_bw.assert_called_once_with(MatchInstance(), 'start', True)
        mock_notify.assert_called_once_with(notifier, self.context,
                                            MatchInstance(),
                                            'exists', system_metadata={},
                                            extra_usage_info=info)

    def test_get_ec2_ids(self):
        expected = {
            'instance-id': 'ec2-inst-id',
            'ami-id': 'ec2-ami-id',
            'kernel-id': 'ami-kernel-ec2-kernelid',
            'ramdisk-id': 'ami-ramdisk-ec2-ramdiskid',
            }
        inst = {
            'uuid': 'fake-uuid',
            'kernel_id': 'ec2-kernelid',
            'ramdisk_id': 'ec2-ramdiskid',
            'image_ref': 'fake-image',
            }
        self.mox.StubOutWithMock(ec2utils, 'id_to_ec2_inst_id')
        self.mox.StubOutWithMock(ec2utils, 'glance_id_to_ec2_id')
        self.mox.StubOutWithMock(ec2utils, 'image_type')

        ec2utils.id_to_ec2_inst_id(inst['uuid']).AndReturn(
            expected['instance-id'])
        ec2utils.glance_id_to_ec2_id(self.context,
                                     inst['image_ref']).AndReturn(
            expected['ami-id'])
        for image_type in ['kernel', 'ramdisk']:
            image_id = inst['%s_id' % image_type]
            ec2utils.image_type(image_type).AndReturn('ami-' + image_type)
            ec2utils.glance_id_to_ec2_id(self.context, image_id,
                                         'ami-' + image_type).AndReturn(
                'ami-%s-ec2-%sid' % (image_type, image_type))

        self.mox.ReplayAll()
        result = self.conductor.get_ec2_ids(self.context, inst)
        self.assertEqual(result, expected)

    def test_migration_get_in_progress_by_host_and_node(self):
        self.mox.StubOutWithMock(db,
                                 'migration_get_in_progress_by_host_and_node')
        db.migration_get_in_progress_by_host_and_node(
            self.context, 'fake-host', 'fake-node').AndReturn('fake-result')
        self.mox.ReplayAll()
        result = self.conductor.migration_get_in_progress_by_host_and_node(
            self.context, 'fake-host', 'fake-node')
        self.assertEqual(result, 'fake-result')

    def test_aggregate_metadata_get_by_host(self):
        self.mox.StubOutWithMock(db, 'aggregate_metadata_get_by_host')
        db.aggregate_metadata_get_by_host(self.context, 'host',
                                          'key').AndReturn('result')
        self.mox.ReplayAll()
        result = self.conductor.aggregate_metadata_get_by_host(self.context,
                                                               'host', 'key')
        self.assertEqual(result, 'result')

    def test_block_device_mapping_get_all_by_instance(self):
        fake_inst = {'uuid': 'fake-uuid'}
        self.mox.StubOutWithMock(db,
                                 'block_device_mapping_get_all_by_instance')
        db.block_device_mapping_get_all_by_instance(
            self.context, fake_inst['uuid']).AndReturn('fake-result')
        self.mox.ReplayAll()
        result = self.conductor.block_device_mapping_get_all_by_instance(
            self.context, fake_inst, legacy=False)
        self.assertEqual(result, 'fake-result')

    def test_compute_node_update(self):
        node = {'id': 'fake-id'}
        self.mox.StubOutWithMock(db, 'compute_node_update')
        db.compute_node_update(self.context, node['id'], {'fake': 'values'}).\
                               AndReturn('fake-result')
        self.mox.ReplayAll()
        result = self.conductor.compute_node_update(self.context, node,
                                                    {'fake': 'values'})
        self.assertEqual(result, 'fake-result')

    def test_compute_node_delete(self):
        node = {'id': 'fake-id'}
        self.mox.StubOutWithMock(db, 'compute_node_delete')
        db.compute_node_delete(self.context, node['id']).AndReturn(None)
        self.mox.ReplayAll()
        result = self.conductor.compute_node_delete(self.context, node)
        self.assertIsNone(result)

    def test_task_log_get(self):
        self.mox.StubOutWithMock(db, 'task_log_get')
        db.task_log_get(self.context, 'task', 'begin', 'end', 'host',
                        'state').AndReturn('result')
        self.mox.ReplayAll()
        result = self.conductor.task_log_get(self.context, 'task', 'begin',
                                             'end', 'host', 'state')
        self.assertEqual(result, 'result')

    def test_task_log_get_with_no_state(self):
        self.mox.StubOutWithMock(db, 'task_log_get')
        db.task_log_get(self.context, 'task', 'begin', 'end',
                        'host', None).AndReturn('result')
        self.mox.ReplayAll()
        result = self.conductor.task_log_get(self.context, 'task', 'begin',
                                             'end', 'host', None)
        self.assertEqual(result, 'result')

    def test_task_log_begin_task(self):
        self.mox.StubOutWithMock(db, 'task_log_begin_task')
        db.task_log_begin_task(self.context.elevated(), 'task', 'begin',
                               'end', 'host', 'items',
                               'message').AndReturn('result')
        self.mox.ReplayAll()
        result = self.conductor.task_log_begin_task(
            self.context, 'task', 'begin', 'end', 'host', 'items', 'message')
        self.assertEqual(result, 'result')

    def test_task_log_end_task(self):
        self.mox.StubOutWithMock(db, 'task_log_end_task')
        db.task_log_end_task(self.context.elevated(), 'task', 'begin', 'end',
                             'host', 'errors', 'message').AndReturn('result')
        self.mox.ReplayAll()
        result = self.conductor.task_log_end_task(
            self.context, 'task', 'begin', 'end', 'host', 'errors', 'message')
        self.assertEqual(result, 'result')

    def test_security_groups_trigger_members_refresh(self):
        self.mox.StubOutWithMock(self.conductor_manager.security_group_api,
                                 'trigger_members_refresh')
        self.conductor_manager.security_group_api.trigger_members_refresh(
            self.context, [1, 2, 3])
        self.mox.ReplayAll()
        self.conductor.security_groups_trigger_members_refresh(self.context,
                                                               [1, 2, 3])

    def test_vol_usage_update(self):
        self.mox.StubOutWithMock(db, 'vol_usage_update')
        self.mox.StubOutWithMock(compute_utils, 'usage_volume_info')

        fake_inst = {'uuid': 'fake-uuid',
                     'project_id': 'fake-project',
                     'user_id': 'fake-user',
                     'availability_zone': 'fake-az',
                     }

        db.vol_usage_update(self.context, 'fake-vol', 22, 33, 44, 55,
                            fake_inst['uuid'],
                            fake_inst['project_id'],
                            fake_inst['user_id'],
                            fake_inst['availability_zone'],
                            False).AndReturn(test_volume_usage.fake_vol_usage)
        compute_utils.usage_volume_info(
            mox.IsA(objects.VolumeUsage)).AndReturn('fake-info')

        self.mox.ReplayAll()

        self.conductor.vol_usage_update(self.context, 'fake-vol',
                                        22, 33, 44, 55, fake_inst, None, False)

        self.assertEqual(1, len(fake_notifier.NOTIFICATIONS))
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual('conductor.%s' % self.conductor_manager.host,
                         msg.publisher_id)
        self.assertEqual('volume.usage', msg.event_type)
        self.assertEqual('INFO', msg.priority)
        self.assertEqual('fake-info', msg.payload)

    def test_compute_node_create(self):
        self.mox.StubOutWithMock(db, 'compute_node_create')
        db.compute_node_create(self.context, 'fake-values').AndReturn(
            'fake-result')
        self.mox.ReplayAll()
        result = self.conductor.compute_node_create(self.context,
                                                    'fake-values')
        self.assertEqual(result, 'fake-result')


class ConductorRPCAPITestCase(_BaseTestCase, test.TestCase):
    """Conductor RPC API Tests."""
    def setUp(self):
        super(ConductorRPCAPITestCase, self).setUp()
        self.conductor_service = self.start_service(
            'conductor', manager='nova.conductor.manager.ConductorManager')
        self.conductor_manager = self.conductor_service.manager
        self.conductor = conductor_rpcapi.ConductorAPI()


class ConductorAPITestCase(_BaseTestCase, test.TestCase):
    """Conductor API Tests."""
    def setUp(self):
        super(ConductorAPITestCase, self).setUp()
        self.conductor_service = self.start_service(
            'conductor', manager='nova.conductor.manager.ConductorManager')
        self.conductor = conductor_api.API()
        self.conductor_manager = self.conductor_service.manager
        self.db = None

    def test_wait_until_ready(self):
        timeouts = []
        calls = dict(count=0)

        def fake_ping(context, message, timeout):
            timeouts.append(timeout)
            calls['count'] += 1
            if calls['count'] < 15:
                raise messaging.MessagingTimeout("fake")

        self.stubs.Set(self.conductor.base_rpcapi, 'ping', fake_ping)

        self.conductor.wait_until_ready(self.context)

        self.assertEqual(timeouts.count(10), 10)
        self.assertIn(None, timeouts)

    @mock.patch('oslo_versionedobjects.base.obj_tree_get_versions')
    def test_object_backport_redirect(self, mock_ovo):
        mock_ovo.return_value = mock.sentinel.obj_versions
        mock_objinst = mock.Mock()

        with mock.patch.object(self.conductor,
                               'object_backport_versions') as mock_call:
            self.conductor.object_backport(mock.sentinel.ctxt,
                                           mock_objinst,
                                           mock.sentinel.target_version)
            mock_call.assert_called_once_with(mock.sentinel.ctxt,
                                              mock_objinst,
                                              mock.sentinel.obj_versions)


class ConductorLocalAPITestCase(ConductorAPITestCase):
    """Conductor LocalAPI Tests."""
    def setUp(self):
        super(ConductorLocalAPITestCase, self).setUp()
        self.conductor = conductor_api.LocalAPI()
        self.conductor_manager = self.conductor._manager._target
        self.db = db

    def test_wait_until_ready(self):
        # Override test in ConductorAPITestCase
        pass


class ConductorImportTest(test.TestCase):
    def test_import_conductor_local(self):
        self.flags(use_local=True, group='conductor')
        self.assertIsInstance(conductor.API(), conductor_api.LocalAPI)
        self.assertIsInstance(conductor.ComputeTaskAPI(),
                              conductor_api.LocalComputeTaskAPI)

    def test_import_conductor_rpc(self):
        self.flags(use_local=False, group='conductor')
        self.assertIsInstance(conductor.API(), conductor_api.API)
        self.assertIsInstance(conductor.ComputeTaskAPI(),
                              conductor_api.ComputeTaskAPI)

    def test_import_conductor_override_to_local(self):
        self.flags(use_local=False, group='conductor')
        self.assertIsInstance(conductor.API(use_local=True),
                              conductor_api.LocalAPI)
        self.assertIsInstance(conductor.ComputeTaskAPI(use_local=True),
                              conductor_api.LocalComputeTaskAPI)


class ConductorPolicyTest(test.TestCase):
    def test_all_allowed_keys(self):
        ctxt = context.RequestContext('fake-user', 'fake-project')
        conductor = conductor_manager.ConductorManager()
        updates = {}
        for key in conductor_manager.allowed_updates:
            if key in conductor_manager.datetime_fields:
                updates[key] = timeutils.utcnow()
            elif key == 'access_ip_v4':
                updates[key] = '10.0.0.2'
            elif key == 'access_ip_v6':
                updates[key] = '2001:db8:0:1::1'
            elif key in ('instance_type_id', 'memory_mb', 'ephemeral_gb',
                         'root_gb', 'vcpus', 'power_state', 'progress'):
                updates[key] = 5
            elif key == 'system_metadata':
                updates[key] = {'foo': 'foo'}
            else:
                updates[key] = 'foo'

        def fake_save(inst):
            # id that comes back from db after updating
            inst.id = 1

        with mock.patch.object(objects.Instance, 'save',
                               side_effect=fake_save,
                               autospec=True) as mock_save:
            conductor.instance_update(ctxt, 'fake-instance', updates,
                                      'conductor')
            mock_save.assert_called_once_with(mock.ANY)

    def test_allowed_keys_are_real(self):
        instance = models.Instance()
        keys = list(conductor_manager.allowed_updates)

        # NOTE(danms): expected_task_state is a parameter that gets
        # passed to the db layer, but is not actually an instance attribute
        del keys[keys.index('expected_task_state')]

        for key in keys:
            self.assertTrue(hasattr(instance, key))


class _BaseTaskTestCase(object):
    def setUp(self):
        super(_BaseTaskTestCase, self).setUp()
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = FakeContext(self.user_id, self.project_id)
        fake_server_actions.stub_out_action_events(self.stubs)

        def fake_deserialize_context(serializer, ctxt_dict):
            self.assertEqual(self.context.user_id, ctxt_dict['user_id'])
            self.assertEqual(self.context.project_id, ctxt_dict['project_id'])
            return self.context

        self.stubs.Set(rpc.RequestContextSerializer, 'deserialize_context',
                       fake_deserialize_context)

    def _prepare_rebuild_args(self, update_args=None):
        # Args that don't get passed in to the method but do get passed to RPC
        migration = update_args and update_args.pop('migration', None)
        node = update_args and update_args.pop('node', None)
        limits = update_args and update_args.pop('limits', None)

        rebuild_args = {'new_pass': 'admin_password',
                        'injected_files': 'files_to_inject',
                        'image_ref': 'image_ref',
                        'orig_image_ref': 'orig_image_ref',
                        'orig_sys_metadata': 'orig_sys_meta',
                        'bdms': {},
                        'recreate': False,
                        'on_shared_storage': False,
                        'preserve_ephemeral': False,
                        'host': 'compute-host'}
        if update_args:
            rebuild_args.update(update_args)
        compute_rebuild_args = copy.deepcopy(rebuild_args)
        compute_rebuild_args['migration'] = migration
        compute_rebuild_args['node'] = node
        compute_rebuild_args['limits'] = limits
        return rebuild_args, compute_rebuild_args

    @mock.patch('nova.objects.Migration')
    def test_live_migrate(self, migobj):
        inst = fake_instance.fake_db_instance()
        inst_obj = objects.Instance._from_db_object(
            self.context, objects.Instance(), inst, [])

        migration = migobj()
        self.mox.StubOutWithMock(live_migrate.LiveMigrationTask, 'execute')
        task = self.conductor_manager._build_live_migrate_task(
            self.context, inst_obj, 'destination', 'block_migration',
            'disk_over_commit', migration)
        task.execute()
        self.mox.ReplayAll()

        if isinstance(self.conductor, (conductor_api.ComputeTaskAPI,
                                       conductor_api.LocalComputeTaskAPI)):
            # The API method is actually 'live_migrate_instance'.  It gets
            # converted into 'migrate_server' when doing RPC.
            self.conductor.live_migrate_instance(self.context, inst_obj,
                'destination', 'block_migration', 'disk_over_commit')
        else:
            self.conductor.migrate_server(self.context, inst_obj,
                {'host': 'destination'}, True, False, None,
                 'block_migration', 'disk_over_commit')

        self.assertEqual('pre-migrating', migration.status)
        self.assertEqual('destination', migration.dest_compute)
        self.assertEqual(inst_obj.host, migration.source_compute)

    def _test_cold_migrate(self, clean_shutdown=True):
        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(scheduler_utils, 'build_request_spec')
        self.mox.StubOutWithMock(migrate.MigrationTask, 'execute')
        inst = fake_instance.fake_db_instance(image_ref='image_ref')
        inst_obj = objects.Instance._from_db_object(
            self.context, objects.Instance(), inst, [])
        inst_obj.system_metadata = {'image_hw_disk_bus': 'scsi'}
        flavor = flavors.get_default_flavor()
        flavor.extra_specs = {'extra_specs': 'fake'}
        filter_properties = {'limits': {},
                             'retry': {'num_attempts': 1,
                                       'hosts': [['host1', None]]}}
        request_spec = {'instance_type': obj_base.obj_to_primitive(flavor),
                        'instance_properties': {}}
        utils.get_image_from_system_metadata(
            inst_obj.system_metadata).AndReturn('image')

        scheduler_utils.build_request_spec(
            self.context, 'image',
            [mox.IsA(objects.Instance)],
            instance_type=mox.IsA(objects.Flavor)).AndReturn(request_spec)
        task = self.conductor_manager._build_cold_migrate_task(
            self.context, inst_obj, flavor, filter_properties,
            request_spec, [], clean_shutdown=clean_shutdown)
        task.execute()
        self.mox.ReplayAll()

        scheduler_hint = {'filter_properties': {}}

        if isinstance(self.conductor, (conductor_api.ComputeTaskAPI,
                                       conductor_api.LocalComputeTaskAPI)):
            # The API method is actually 'resize_instance'.  It gets
            # converted into 'migrate_server' when doing RPC.
            self.conductor.resize_instance(
                self.context, inst_obj, {}, scheduler_hint, flavor, [],
                clean_shutdown)
        else:
            self.conductor.migrate_server(
                self.context, inst_obj, scheduler_hint,
                False, False, flavor, None, None, [],
                clean_shutdown)

    def test_cold_migrate(self):
        self._test_cold_migrate()

    def test_cold_migrate_forced_shutdown(self):
        self._test_cold_migrate(clean_shutdown=False)

    @mock.patch('nova.objects.Instance.refresh')
    @mock.patch('nova.utils.spawn_n')
    def test_build_instances(self, mock_spawn, mock_refresh):
        mock_spawn.side_effect = lambda f, *a, **k: f(*a, **k)
        instance_type = flavors.get_default_flavor()
        instances = [objects.Instance(context=self.context,
                                      id=i,
                                      uuid=uuid.uuid4(),
                                      flavor=instance_type) for i in range(2)]
        instance_type_p = obj_base.obj_to_primitive(instance_type)
        instance_properties = obj_base.obj_to_primitive(instances[0])
        instance_properties['system_metadata'] = flavors.save_flavor_info(
            {}, instance_type)

        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.conductor_manager.scheduler_client,
                                 'select_destinations')
        self.mox.StubOutWithMock(db,
                                 'block_device_mapping_get_all_by_instance')
        self.mox.StubOutWithMock(self.conductor_manager.compute_rpcapi,
                                 'build_and_run_instance')

        spec = {'image': {'fake_data': 'should_pass_silently'},
                'instance_properties': instance_properties,
                'instance_type': instance_type_p,
                'num_instances': 2}
        scheduler_utils.setup_instance_group(self.context, spec, {})
        self.conductor_manager.scheduler_client.select_destinations(
                self.context, spec,
                {'retry': {'num_attempts': 1, 'hosts': []}}).AndReturn(
                        [{'host': 'host1', 'nodename': 'node1', 'limits': []},
                         {'host': 'host2', 'nodename': 'node2', 'limits': []}])
        db.block_device_mapping_get_all_by_instance(self.context,
                instances[0].uuid, use_subordinate=False).AndReturn([])
        self.conductor_manager.compute_rpcapi.build_and_run_instance(
                self.context,
                instance=mox.IgnoreArg(),
                host='host1',
                image={'fake_data': 'should_pass_silently'},
                request_spec={
                    'image': {'fake_data': 'should_pass_silently'},
                    'instance_properties': instance_properties,
                    'instance_type': instance_type_p,
                    'num_instances': 2},
                filter_properties={'retry': {'num_attempts': 1,
                                             'hosts': [['host1', 'node1']]},
                                   'limits': []},
                admin_password='admin_password',
                injected_files='injected_files',
                requested_networks=None,
                security_groups='security_groups',
                block_device_mapping=mox.IgnoreArg(),
                node='node1', limits=[])
        db.block_device_mapping_get_all_by_instance(self.context,
                instances[1].uuid, use_subordinate=False).AndReturn([])
        self.conductor_manager.compute_rpcapi.build_and_run_instance(
                self.context,
                instance=mox.IgnoreArg(),
                host='host2',
                image={'fake_data': 'should_pass_silently'},
                request_spec={
                    'image': {'fake_data': 'should_pass_silently'},
                    'instance_properties': instance_properties,
                    'instance_type': instance_type_p,
                    'num_instances': 2},
                filter_properties={'limits': [],
                                   'retry': {'num_attempts': 1,
                                             'hosts': [['host2', 'node2']]}},
                admin_password='admin_password',
                injected_files='injected_files',
                requested_networks=None,
                security_groups='security_groups',
                block_device_mapping=mox.IgnoreArg(),
                node='node2', limits=[])
        self.mox.ReplayAll()

        # build_instances() is a cast, we need to wait for it to complete
        self.useFixture(cast_as_call.CastAsCall(self.stubs))

        self.conductor.build_instances(self.context,
                instances=instances,
                image={'fake_data': 'should_pass_silently'},
                filter_properties={},
                admin_password='admin_password',
                injected_files='injected_files',
                requested_networks=None,
                security_groups='security_groups',
                block_device_mapping='block_device_mapping',
                legacy_bdm=False)

    def test_build_instances_scheduler_failure(self):
        instances = [fake_instance.fake_instance_obj(self.context)
                for i in range(2)]
        image = {'fake-data': 'should_pass_silently'}
        spec = {'fake': 'specs',
                'instance_properties': instances[0]}
        exception = exc.NoValidHost(reason='fake-reason')
        self.mox.StubOutWithMock(scheduler_utils, 'build_request_spec')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(scheduler_utils, 'set_vm_state_and_notify')
        self.mox.StubOutWithMock(self.conductor_manager.scheduler_client,
                'select_destinations')

        scheduler_utils.build_request_spec(self.context, image,
                mox.IgnoreArg()).AndReturn(spec)
        scheduler_utils.setup_instance_group(self.context, spec, {})
        self.conductor_manager.scheduler_client.select_destinations(
                self.context, spec,
                {'retry': {'num_attempts': 1,
                           'hosts': []}}).AndRaise(exception)
        updates = {'vm_state': vm_states.ERROR, 'task_state': None}
        for instance in instances:
            scheduler_utils.set_vm_state_and_notify(
                self.context, instance.uuid, 'compute_task', 'build_instances',
                updates, exception, spec, self.conductor_manager.db)
        self.mox.ReplayAll()

        # build_instances() is a cast, we need to wait for it to complete
        self.useFixture(cast_as_call.CastAsCall(self.stubs))

        self.conductor.build_instances(self.context,
                instances=instances,
                image=image,
                filter_properties={},
                admin_password='admin_password',
                injected_files='injected_files',
                requested_networks=None,
                security_groups='security_groups',
                block_device_mapping='block_device_mapping',
                legacy_bdm=False)

    @mock.patch('nova.utils.spawn_n')
    @mock.patch.object(scheduler_utils, 'build_request_spec')
    @mock.patch.object(scheduler_utils, 'setup_instance_group')
    @mock.patch.object(conductor_manager.ComputeTaskManager,
                       '_set_vm_state_and_notify')
    def test_build_instances_scheduler_group_failure(self, state_mock,
                                                     sig_mock, bs_mock,
                                                     spawn_mock):
        instances = [fake_instance.fake_instance_obj(self.context)
                     for i in range(2)]
        image = {'fake-data': 'should_pass_silently'}
        spec = {'fake': 'specs',
                'instance_properties': instances[0]}

        # NOTE(gibi): LocalComputeTaskAPI use eventlet spawn that makes mocking
        # hard so use direct call instead.
        spawn_mock.side_effect = lambda f, *a, **k: f(*a, **k)
        bs_mock.return_value = spec
        exception = exc.UnsupportedPolicyException(reason='fake-reason')
        sig_mock.side_effect = exception

        updates = {'vm_state': vm_states.ERROR, 'task_state': None}

        # build_instances() is a cast, we need to wait for it to complete
        self.useFixture(cast_as_call.CastAsCall(self.stubs))

        self.conductor.build_instances(
                          context=self.context,
                          instances=instances,
                          image=image,
                          filter_properties={},
                          admin_password='admin_password',
                          injected_files='injected_files',
                          requested_networks=None,
                          security_groups='security_groups',
                          block_device_mapping='block_device_mapping',
                          legacy_bdm=False)
        calls = []
        for instance in instances:
            calls.append(mock.call(self.context, instance.uuid,
                         'build_instances', updates, exception, spec))
        state_mock.assert_has_calls(calls)

    def test_unshelve_instance_on_host(self):
        instance = self._create_fake_instance_obj()
        instance.vm_state = vm_states.SHELVED
        instance.task_state = task_states.UNSHELVING
        instance.save()
        system_metadata = instance.system_metadata

        self.mox.StubOutWithMock(self.conductor_manager.compute_rpcapi,
                'start_instance')
        self.mox.StubOutWithMock(self.conductor_manager.compute_rpcapi,
                'unshelve_instance')

        self.conductor_manager.compute_rpcapi.start_instance(self.context,
                instance)
        self.mox.ReplayAll()

        system_metadata['shelved_at'] = timeutils.utcnow()
        system_metadata['shelved_image_id'] = 'fake_image_id'
        system_metadata['shelved_host'] = 'fake-mini'
        self.conductor_manager.unshelve_instance(self.context, instance)

    def test_unshelve_offloaded_instance_glance_image_not_found(self):
        shelved_image_id = "image_not_found"

        instance = self._create_fake_instance_obj()
        instance.vm_state = vm_states.SHELVED_OFFLOADED
        instance.task_state = task_states.UNSHELVING
        instance.save()
        system_metadata = instance.system_metadata

        self.mox.StubOutWithMock(self.conductor_manager.image_api, 'get')

        e = exc.ImageNotFound(image_id=shelved_image_id)
        self.conductor_manager.image_api.get(
            self.context, shelved_image_id, show_deleted=False).AndRaise(e)
        self.mox.ReplayAll()

        system_metadata['shelved_at'] = timeutils.utcnow()
        system_metadata['shelved_host'] = 'fake-mini'
        system_metadata['shelved_image_id'] = shelved_image_id

        self.assertRaises(
            exc.UnshelveException,
            self.conductor_manager.unshelve_instance,
            self.context, instance)
        self.assertEqual(instance.vm_state, vm_states.ERROR)

    def test_unshelve_offloaded_instance_image_id_is_none(self):

        instance = self._create_fake_instance_obj()
        instance.vm_state = vm_states.SHELVED_OFFLOADED
        instance.task_state = task_states.UNSHELVING
        # 'shelved_image_id' is None for volumebacked instance
        instance.system_metadata['shelved_image_id'] = None

        with contextlib.nested(
            mock.patch.object(self.conductor_manager,
                              '_schedule_instances'),
            mock.patch.object(self.conductor_manager.compute_rpcapi,
                              'unshelve_instance'),
        ) as (schedule_mock, unshelve_mock):
            schedule_mock.return_value = [{'host': 'fake_host',
                                           'nodename': 'fake_node',
                                           'limits': {}}]
            self.conductor_manager.unshelve_instance(self.context, instance)
            self.assertEqual(1, unshelve_mock.call_count)

    def test_unshelve_instance_schedule_and_rebuild(self):
        instance = self._create_fake_instance_obj()
        instance.vm_state = vm_states.SHELVED_OFFLOADED
        instance.save()
        filter_properties = {'retry': {'num_attempts': 1,
                                       'hosts': []}}
        system_metadata = instance.system_metadata

        self.mox.StubOutWithMock(self.conductor_manager.image_api, 'get')
        self.mox.StubOutWithMock(self.conductor_manager, '_schedule_instances')
        self.mox.StubOutWithMock(self.conductor_manager.compute_rpcapi,
                'unshelve_instance')

        self.conductor_manager.image_api.get(self.context,
                'fake_image_id', show_deleted=False).AndReturn('fake_image')
        self.conductor_manager._schedule_instances(self.context,
                'fake_image', filter_properties, instance).AndReturn(
                        [{'host': 'fake_host',
                          'nodename': 'fake_node',
                          'limits': {}}])
        self.conductor_manager.compute_rpcapi.unshelve_instance(self.context,
                instance, 'fake_host', image='fake_image',
                filter_properties={'limits': {},
                                   'retry': {'num_attempts': 1,
                                             'hosts': [['fake_host',
                                                        'fake_node']]}},
                                    node='fake_node')
        self.mox.ReplayAll()

        system_metadata['shelved_at'] = timeutils.utcnow()
        system_metadata['shelved_image_id'] = 'fake_image_id'
        system_metadata['shelved_host'] = 'fake-mini'
        self.conductor_manager.unshelve_instance(self.context, instance)

    def test_unshelve_instance_schedule_and_rebuild_novalid_host(self):
        instance = self._create_fake_instance_obj()
        instance.vm_state = vm_states.SHELVED_OFFLOADED
        instance.save()
        system_metadata = instance.system_metadata

        def fake_schedule_instances(context, image, filter_properties,
                                    *instances):
            raise exc.NoValidHost(reason='')

        with contextlib.nested(
            mock.patch.object(self.conductor_manager.image_api, 'get',
                              return_value='fake_image'),
            mock.patch.object(self.conductor_manager, '_schedule_instances',
                              fake_schedule_instances)
        ) as (_get_image, _schedule_instances):
            system_metadata['shelved_at'] = timeutils.utcnow()
            system_metadata['shelved_image_id'] = 'fake_image_id'
            system_metadata['shelved_host'] = 'fake-mini'
            self.conductor_manager.unshelve_instance(self.context, instance)
            _get_image.assert_has_calls([mock.call(self.context,
                                      system_metadata['shelved_image_id'],
                                      show_deleted=False)])
            self.assertEqual(vm_states.SHELVED_OFFLOADED, instance.vm_state)

    @mock.patch.object(conductor_manager.ComputeTaskManager,
                       '_schedule_instances',
                       side_effect=messaging.MessagingTimeout())
    @mock.patch.object(image_api.API, 'get', return_value='fake_image')
    def test_unshelve_instance_schedule_and_rebuild_messaging_exception(
            self, mock_get_image, mock_schedule_instances):
        instance = self._create_fake_instance_obj()
        instance.vm_state = vm_states.SHELVED_OFFLOADED
        instance.task_state = task_states.UNSHELVING
        instance.save()
        system_metadata = instance.system_metadata

        system_metadata['shelved_at'] = timeutils.utcnow()
        system_metadata['shelved_image_id'] = 'fake_image_id'
        system_metadata['shelved_host'] = 'fake-mini'
        self.assertRaises(messaging.MessagingTimeout,
                          self.conductor_manager.unshelve_instance,
                          self.context, instance)
        mock_get_image.assert_has_calls([mock.call(self.context,
                                        system_metadata['shelved_image_id'],
                                        show_deleted=False)])
        self.assertEqual(vm_states.SHELVED_OFFLOADED, instance.vm_state)
        self.assertIsNone(instance.task_state)

    def test_unshelve_instance_schedule_and_rebuild_volume_backed(self):
        instance = self._create_fake_instance_obj()
        instance.vm_state = vm_states.SHELVED_OFFLOADED
        instance.save()
        filter_properties = {'retry': {'num_attempts': 1,
                                       'hosts': []}}
        system_metadata = instance.system_metadata

        self.mox.StubOutWithMock(self.conductor_manager, '_schedule_instances')
        self.mox.StubOutWithMock(self.conductor_manager.compute_rpcapi,
                'unshelve_instance')

        self.conductor_manager._schedule_instances(self.context,
                None, filter_properties, instance).AndReturn(
                        [{'host': 'fake_host',
                          'nodename': 'fake_node',
                          'limits': {}}])
        self.conductor_manager.compute_rpcapi.unshelve_instance(self.context,
                instance, 'fake_host', image=None,
                filter_properties={'limits': {},
                                   'retry': {'num_attempts': 1,
                                             'hosts': [['fake_host',
                                                        'fake_node']]}},
                node='fake_node')
        self.mox.ReplayAll()

        system_metadata['shelved_at'] = timeutils.utcnow()
        system_metadata['shelved_host'] = 'fake-mini'
        self.conductor_manager.unshelve_instance(self.context, instance)

    def test_rebuild_instance(self):
        inst_obj = self._create_fake_instance_obj()
        rebuild_args, compute_args = self._prepare_rebuild_args(
            {'host': inst_obj.host})

        with contextlib.nested(
            mock.patch.object(self.conductor_manager.compute_rpcapi,
                              'rebuild_instance'),
            mock.patch.object(self.conductor_manager.scheduler_client,
                              'select_destinations')
        ) as (rebuild_mock, select_dest_mock):
            self.conductor_manager.rebuild_instance(context=self.context,
                                            instance=inst_obj,
                                            **rebuild_args)
            self.assertFalse(select_dest_mock.called)
            rebuild_mock.assert_called_once_with(self.context,
                               instance=inst_obj,
                               **compute_args)

    def test_rebuild_instance_with_scheduler(self):
        inst_obj = self._create_fake_instance_obj()
        inst_obj.host = 'noselect'
        expected_host = 'thebesthost'
        expected_node = 'thebestnode'
        expected_limits = 'fake-limits'
        rebuild_args, compute_args = self._prepare_rebuild_args(
            {'host': None, 'node': expected_node, 'limits': expected_limits})
        request_spec = {}
        filter_properties = {'ignore_hosts': [(inst_obj.host)]}

        with contextlib.nested(
            mock.patch.object(self.conductor_manager.compute_rpcapi,
                              'rebuild_instance'),
            mock.patch.object(scheduler_utils, 'setup_instance_group',
                              return_value=False),
            mock.patch.object(self.conductor_manager.scheduler_client,
                              'select_destinations',
                              return_value=[{'host': expected_host,
                                             'nodename': expected_node,
                                             'limits': expected_limits}]),
            mock.patch('nova.scheduler.utils.build_request_spec',
                       return_value=request_spec)
        ) as (rebuild_mock, sig_mock, select_dest_mock, bs_mock):
            self.conductor_manager.rebuild_instance(context=self.context,
                                            instance=inst_obj,
                                            **rebuild_args)
            select_dest_mock.assert_called_once_with(self.context,
                                                     request_spec,
                                                     filter_properties)
            compute_args['host'] = expected_host
            rebuild_mock.assert_called_once_with(self.context,
                                            instance=inst_obj,
                                            **compute_args)
        self.assertEqual('compute.instance.rebuild.scheduled',
                         fake_notifier.NOTIFICATIONS[0].event_type)

    def test_rebuild_instance_with_scheduler_no_host(self):
        inst_obj = self._create_fake_instance_obj()
        inst_obj.host = 'noselect'
        rebuild_args, _ = self._prepare_rebuild_args({'host': None})
        request_spec = {}
        filter_properties = {'ignore_hosts': [(inst_obj.host)]}

        with contextlib.nested(
            mock.patch.object(self.conductor_manager.compute_rpcapi,
                              'rebuild_instance'),
            mock.patch.object(scheduler_utils, 'setup_instance_group',
                              return_value=False),
            mock.patch.object(self.conductor_manager.scheduler_client,
                              'select_destinations',
                              side_effect=exc.NoValidHost(reason='')),
            mock.patch('nova.scheduler.utils.build_request_spec',
                       return_value=request_spec)
        ) as (rebuild_mock, sig_mock, select_dest_mock, bs_mock):
            self.assertRaises(exc.NoValidHost,
                              self.conductor_manager.rebuild_instance,
                              context=self.context, instance=inst_obj,
                              **rebuild_args)
            select_dest_mock.assert_called_once_with(self.context,
                                                     request_spec,
                                                     filter_properties)
            self.assertFalse(rebuild_mock.called)

    @mock.patch('nova.utils.spawn_n')
    @mock.patch.object(conductor_manager.compute_rpcapi.ComputeAPI,
                       'rebuild_instance')
    @mock.patch.object(scheduler_utils, 'setup_instance_group')
    @mock.patch.object(conductor_manager.scheduler_client.SchedulerClient,
                       'select_destinations')
    @mock.patch('nova.scheduler.utils.build_request_spec')
    @mock.patch.object(conductor_manager.ComputeTaskManager,
                       '_set_vm_state_and_notify')
    def test_rebuild_instance_with_scheduler_group_failure(self,
                                                           state_mock,
                                                           bs_mock,
                                                           select_dest_mock,
                                                           sig_mock,
                                                           rebuild_mock,
                                                           spawn_mock):
        inst_obj = self._create_fake_instance_obj()
        rebuild_args, _ = self._prepare_rebuild_args({'host': None})
        request_spec = {}
        bs_mock.return_value = request_spec

        # NOTE(gibi): LocalComputeTaskAPI use eventlet spawn that makes mocking
        # hard so use direct call instead.
        spawn_mock.side_effect = lambda f, *a, **k: f(*a, **k)

        exception = exc.UnsupportedPolicyException(reason='')
        sig_mock.side_effect = exception

        # build_instances() is a cast, we need to wait for it to complete
        self.useFixture(cast_as_call.CastAsCall(self.stubs))

        self.assertRaises(exc.UnsupportedPolicyException,
                          self.conductor.rebuild_instance,
                          self.context,
                          inst_obj,
                          **rebuild_args)
        updates = {'vm_state': vm_states.ACTIVE, 'task_state': None}
        state_mock.assert_called_once_with(self.context, inst_obj.uuid,
                                           'rebuild_server', updates,
                                           exception, request_spec)
        self.assertFalse(select_dest_mock.called)
        self.assertFalse(rebuild_mock.called)

    def test_rebuild_instance_evacuate_migration_record(self):
        inst_obj = self._create_fake_instance_obj()
        migration = objects.Migration(context=self.context,
                                      source_compute=inst_obj.host,
                                      source_node=inst_obj.node,
                                      instance_uuid=inst_obj.uuid,
                                      status='accepted',
                                      migration_type='evacuation')
        rebuild_args, compute_args = self._prepare_rebuild_args(
            {'host': inst_obj.host, 'migration': migration})

        with contextlib.nested(
            mock.patch.object(self.conductor_manager.compute_rpcapi,
                              'rebuild_instance'),
            mock.patch.object(self.conductor_manager.scheduler_client,
                              'select_destinations'),
            mock.patch.object(objects.Migration, 'get_by_instance_and_status',
                              return_value=migration)
        ) as (rebuild_mock, select_dest_mock, get_migration_mock):
            self.conductor_manager.rebuild_instance(context=self.context,
                                            instance=inst_obj,
                                            **rebuild_args)
            self.assertFalse(select_dest_mock.called)
            rebuild_mock.assert_called_once_with(self.context,
                               instance=inst_obj,
                               **compute_args)


class ConductorTaskTestCase(_BaseTaskTestCase, test_compute.BaseTestCase):
    """ComputeTaskManager Tests."""
    def setUp(self):
        super(ConductorTaskTestCase, self).setUp()
        self.conductor = conductor_manager.ComputeTaskManager()
        self.conductor_manager = self.conductor

    def test_migrate_server_fails_with_rebuild(self):
        self.assertRaises(NotImplementedError, self.conductor.migrate_server,
            self.context, None, None, True, True, None, None, None)

    def test_migrate_server_fails_with_flavor(self):
        flavor = flavors.get_flavor_by_name('m1.tiny')
        self.assertRaises(NotImplementedError, self.conductor.migrate_server,
            self.context, None, None, True, False, flavor, None, None)

    def _build_request_spec(self, instance):
        return {
            'instance_properties': {
                'uuid': instance['uuid'], },
        }

    @mock.patch.object(scheduler_utils, 'set_vm_state_and_notify')
    @mock.patch.object(live_migrate.LiveMigrationTask, 'execute')
    def _test_migrate_server_deals_with_expected_exceptions(self, ex,
        mock_execute, mock_set):
        instance = fake_instance.fake_db_instance(uuid='uuid',
                                                  vm_state=vm_states.ACTIVE)
        inst_obj = objects.Instance._from_db_object(
            self.context, objects.Instance(), instance, [])
        mock_execute.side_effect = ex
        self.conductor = utils.ExceptionHelper(self.conductor)

        self.assertRaises(type(ex),
            self.conductor.migrate_server, self.context, inst_obj,
            {'host': 'destination'}, True, False, None, 'block_migration',
            'disk_over_commit')

        mock_set.assert_called_once_with(self.context,
                inst_obj.uuid,
                'compute_task', 'migrate_server',
                {'vm_state': vm_states.ACTIVE,
                 'task_state': None,
                 'expected_task_state': task_states.MIGRATING},
                ex, self._build_request_spec(inst_obj),
                self.conductor_manager.db)

    def test_migrate_server_deals_with_invalidcpuinfo_exception(self):
        instance = fake_instance.fake_db_instance(uuid='uuid',
                                                  vm_state=vm_states.ACTIVE)
        inst_obj = objects.Instance._from_db_object(
            self.context, objects.Instance(), instance, [])
        self.mox.StubOutWithMock(live_migrate.LiveMigrationTask, 'execute')
        self.mox.StubOutWithMock(scheduler_utils,
                'set_vm_state_and_notify')

        ex = exc.InvalidCPUInfo(reason="invalid cpu info.")

        task = self.conductor._build_live_migrate_task(
            self.context, inst_obj, 'destination', 'block_migration',
            'disk_over_commit', mox.IsA(objects.Migration))
        task.execute().AndRaise(ex)

        scheduler_utils.set_vm_state_and_notify(self.context,
                inst_obj.uuid,
                'compute_task', 'migrate_server',
                {'vm_state': vm_states.ACTIVE,
                 'task_state': None,
                 'expected_task_state': task_states.MIGRATING},
                ex, self._build_request_spec(inst_obj),
                self.conductor_manager.db)
        self.mox.ReplayAll()

        self.conductor = utils.ExceptionHelper(self.conductor)

        self.assertRaises(exc.InvalidCPUInfo,
            self.conductor.migrate_server, self.context, inst_obj,
            {'host': 'destination'}, True, False, None, 'block_migration',
            'disk_over_commit')

    def test_migrate_server_deals_with_expected_exception(self):
        exs = [exc.InstanceInvalidState(instance_uuid="fake", attr='',
                                        state='', method=''),
               exc.DestinationHypervisorTooOld(),
               exc.HypervisorUnavailable(host='dummy'),
               exc.LiveMigrationWithOldNovaNotSafe(server='dummy'),
               exc.MigrationPreCheckError(reason='dummy'),
               exc.InvalidSharedStorage(path='dummy', reason='dummy'),
               exc.NoValidHost(reason='dummy'),
               exc.ComputeServiceUnavailable(host='dummy'),
               exc.InvalidHypervisorType(),
               exc.InvalidCPUInfo(reason='dummy'),
               exc.UnableToMigrateToSelf(instance_id='dummy', host='dummy'),
               exc.InvalidLocalStorage(path='dummy', reason='dummy')]
        for ex in exs:
            self._test_migrate_server_deals_with_expected_exceptions(ex)

    @mock.patch.object(scheduler_utils, 'set_vm_state_and_notify')
    @mock.patch.object(live_migrate.LiveMigrationTask, 'execute')
    def test_migrate_server_deals_with_unexpected_exceptions(self,
            mock_live_migrate, mock_set_state):
        expected_ex = IOError('fake error')
        mock_live_migrate.side_effect = expected_ex
        instance = fake_instance.fake_db_instance()
        inst_obj = objects.Instance._from_db_object(
            self.context, objects.Instance(), instance, [])
        ex = self.assertRaises(exc.MigrationError,
            self.conductor.migrate_server, self.context, inst_obj,
            {'host': 'destination'}, True, False, None, 'block_migration',
            'disk_over_commit')
        request_spec = {'instance_properties': {
                'uuid': instance['uuid'], },
        }
        mock_set_state.assert_called_once_with(self.context,
                        instance['uuid'],
                        'compute_task', 'migrate_server',
                        dict(vm_state=vm_states.ERROR,
                             task_state=inst_obj.task_state,
                             expected_task_state=task_states.MIGRATING,),
                        expected_ex, request_spec, self.conductor.db)
        self.assertEqual(ex.kwargs['reason'], six.text_type(expected_ex))

    def test_set_vm_state_and_notify(self):
        self.mox.StubOutWithMock(scheduler_utils,
                                 'set_vm_state_and_notify')
        scheduler_utils.set_vm_state_and_notify(
                self.context, 1, 'compute_task', 'method', 'updates',
                'ex', 'request_spec', self.conductor.db)

        self.mox.ReplayAll()

        self.conductor._set_vm_state_and_notify(
                self.context, 1, 'method', 'updates', 'ex', 'request_spec')

    @mock.patch.object(scheduler_utils, 'build_request_spec')
    @mock.patch.object(scheduler_utils, 'setup_instance_group')
    @mock.patch.object(utils, 'get_image_from_system_metadata')
    @mock.patch.object(objects.Quotas, 'from_reservations')
    @mock.patch.object(scheduler_client.SchedulerClient, 'select_destinations')
    @mock.patch.object(conductor_manager.ComputeTaskManager,
                       '_set_vm_state_and_notify')
    @mock.patch.object(migrate.MigrationTask, 'rollback')
    def test_cold_migrate_no_valid_host_back_in_active_state(
            self, rollback_mock, notify_mock, select_dest_mock, quotas_mock,
            metadata_mock, sig_mock, brs_mock):
        flavor = flavors.get_flavor_by_name('m1.tiny')
        inst_obj = objects.Instance(
            image_ref='fake-image_ref',
            instance_type_id=flavor['id'],
            vm_state=vm_states.ACTIVE,
            system_metadata={},
            uuid='fake',
            user_id='fake')
        request_spec = dict(instance_type=dict(extra_specs=dict()),
                            instance_properties=dict())
        filter_props = dict(context=None)
        resvs = 'fake-resvs'
        image = 'fake-image'
        metadata_mock.return_value = image
        brs_mock.return_value = request_spec
        exc_info = exc.NoValidHost(reason="")
        select_dest_mock.side_effect = exc_info
        updates = {'vm_state': vm_states.ACTIVE,
                   'task_state': None}
        self.assertRaises(exc.NoValidHost,
                          self.conductor._cold_migrate,
                          self.context, inst_obj,
                          flavor, filter_props, [resvs],
                          clean_shutdown=True)
        metadata_mock.assert_called_with({})
        brs_mock.assert_called_once_with(self.context, image,
                                         [inst_obj],
                                         instance_type=flavor)
        quotas_mock.assert_called_once_with(self.context, [resvs],
                                            instance=inst_obj)
        sig_mock.assert_called_once_with(self.context, request_spec,
                                         filter_props)
        notify_mock.assert_called_once_with(self.context, inst_obj.uuid,
                                              'migrate_server', updates,
                                              exc_info, request_spec)
        rollback_mock.assert_called_once_with()

    @mock.patch.object(scheduler_utils, 'build_request_spec')
    @mock.patch.object(scheduler_utils, 'setup_instance_group')
    @mock.patch.object(utils, 'get_image_from_system_metadata')
    @mock.patch.object(objects.Quotas, 'from_reservations')
    @mock.patch.object(scheduler_client.SchedulerClient, 'select_destinations')
    @mock.patch.object(conductor_manager.ComputeTaskManager,
                       '_set_vm_state_and_notify')
    @mock.patch.object(migrate.MigrationTask, 'rollback')
    def test_cold_migrate_no_valid_host_back_in_stopped_state(
            self, rollback_mock, notify_mock, select_dest_mock, quotas_mock,
            metadata_mock, sig_mock, brs_mock):
        flavor = flavors.get_flavor_by_name('m1.tiny')
        inst_obj = objects.Instance(
            image_ref='fake-image_ref',
            vm_state=vm_states.STOPPED,
            instance_type_id=flavor['id'],
            system_metadata={},
            uuid='fake',
            user_id='fake')
        image = 'fake-image'
        request_spec = dict(instance_type=dict(extra_specs=dict()),
                            instance_properties=dict(),
                            image=image)
        filter_props = dict(context=None)
        resvs = 'fake-resvs'

        metadata_mock.return_value = image
        brs_mock.return_value = request_spec
        exc_info = exc.NoValidHost(reason="")
        select_dest_mock.side_effect = exc_info
        updates = {'vm_state': vm_states.STOPPED,
                   'task_state': None}
        self.assertRaises(exc.NoValidHost,
                           self.conductor._cold_migrate,
                           self.context, inst_obj,
                           flavor, filter_props, [resvs],
                           clean_shutdown=True)
        metadata_mock.assert_called_with({})
        brs_mock.assert_called_once_with(self.context, image,
                                                     [inst_obj],
                                                     instance_type=flavor)
        quotas_mock.assert_called_once_with(self.context, [resvs],
                                            instance=inst_obj)
        sig_mock.assert_called_once_with(self.context, request_spec,
                                         filter_props)
        notify_mock.assert_called_once_with(self.context, inst_obj.uuid,
                                            'migrate_server', updates,
                                            exc_info, request_spec)
        rollback_mock.assert_called_once_with()

    def test_cold_migrate_no_valid_host_error_msg(self):
        flavor = flavors.get_flavor_by_name('m1.tiny')
        inst_obj = objects.Instance(
            image_ref='fake-image_ref',
            vm_state=vm_states.STOPPED,
            instance_type_id=flavor['id'],
            system_metadata={},
            uuid='fake',
            user_id='fake')
        request_spec = dict(instance_type=dict(extra_specs=dict()),
                            instance_properties=dict())
        filter_props = dict(context=None)
        resvs = 'fake-resvs'
        image = 'fake-image'

        with contextlib.nested(
            mock.patch.object(utils, 'get_image_from_system_metadata',
                              return_value=image),
            mock.patch.object(scheduler_utils, 'build_request_spec',
                              return_value=request_spec),
            mock.patch.object(self.conductor, '_set_vm_state_and_notify'),
            mock.patch.object(migrate.MigrationTask,
                              'execute',
                              side_effect=exc.NoValidHost(reason="")),
            mock.patch.object(migrate.MigrationTask, 'rollback')
        ) as (image_mock, brs_mock, set_vm_mock, task_execute_mock,
              task_rollback_mock):
            nvh = self.assertRaises(exc.NoValidHost,
                                    self.conductor._cold_migrate, self.context,
                                    inst_obj, flavor, filter_props, [resvs],
                                    clean_shutdown=True)
            self.assertIn('cold migrate', nvh.message)

    @mock.patch.object(utils, 'get_image_from_system_metadata')
    @mock.patch('nova.scheduler.utils.build_request_spec')
    @mock.patch.object(migrate.MigrationTask, 'execute')
    @mock.patch.object(migrate.MigrationTask, 'rollback')
    @mock.patch.object(conductor_manager.ComputeTaskManager,
                       '_set_vm_state_and_notify')
    def test_cold_migrate_no_valid_host_in_group(self,
                                                 set_vm_mock,
                                                 task_rollback_mock,
                                                 task_exec_mock,
                                                 brs_mock,
                                                 image_mock):
        flavor = flavors.get_flavor_by_name('m1.tiny')
        inst_obj = objects.Instance(
            image_ref='fake-image_ref',
            vm_state=vm_states.STOPPED,
            instance_type_id=flavor['id'],
            system_metadata={},
            uuid='fake',
            user_id='fake')
        request_spec = dict(instance_type=dict(extra_specs=dict()),
                            instance_properties=dict())
        filter_props = dict(context=None)
        resvs = 'fake-resvs'
        image = 'fake-image'
        exception = exc.UnsupportedPolicyException(reason='')

        image_mock.return_value = image
        brs_mock.return_value = request_spec
        task_exec_mock.side_effect = exception

        self.assertRaises(exc.UnsupportedPolicyException,
                          self.conductor._cold_migrate, self.context,
                          inst_obj, flavor, filter_props, [resvs],
                          clean_shutdown=True)

        updates = {'vm_state': vm_states.STOPPED, 'task_state': None}
        set_vm_mock.assert_called_once_with(self.context, inst_obj.uuid,
                                            'migrate_server', updates,
                                            exception, request_spec)

    @mock.patch.object(scheduler_utils, 'build_request_spec')
    @mock.patch.object(scheduler_utils, 'setup_instance_group')
    @mock.patch.object(utils, 'get_image_from_system_metadata')
    @mock.patch.object(objects.Quotas, 'from_reservations')
    @mock.patch.object(scheduler_client.SchedulerClient, 'select_destinations')
    @mock.patch.object(conductor_manager.ComputeTaskManager,
                       '_set_vm_state_and_notify')
    @mock.patch.object(migrate.MigrationTask, 'rollback')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'prep_resize')
    def test_cold_migrate_exception_host_in_error_state_and_raise(
            self, prep_resize_mock, rollback_mock, notify_mock,
            select_dest_mock, quotas_mock, metadata_mock, sig_mock, brs_mock):
        flavor = flavors.get_flavor_by_name('m1.tiny')
        inst_obj = objects.Instance(
            image_ref='fake-image_ref',
            vm_state=vm_states.STOPPED,
            instance_type_id=flavor['id'],
            system_metadata={},
            uuid='fake',
            user_id='fake')
        image = 'fake-image'
        request_spec = dict(instance_type=dict(),
                            instance_properties=dict(),
                            image=image)
        filter_props = dict(context=None)
        resvs = 'fake-resvs'

        hosts = [dict(host='host1', nodename=None, limits={})]
        metadata_mock.return_value = image
        brs_mock.return_value = request_spec
        exc_info = test.TestingException('something happened')
        select_dest_mock.return_value = hosts

        updates = {'vm_state': vm_states.STOPPED,
                   'task_state': None}
        prep_resize_mock.side_effect = exc_info
        self.assertRaises(test.TestingException,
                          self.conductor._cold_migrate,
                          self.context, inst_obj, flavor,
                          filter_props, [resvs],
                          clean_shutdown=True)

        metadata_mock.assert_called_with({})
        brs_mock.assert_called_once_with(self.context, image,
                                                     [inst_obj],
                                                     instance_type=flavor)
        quotas_mock.assert_called_once_with(self.context, [resvs],
                                            instance=inst_obj)
        sig_mock.assert_called_once_with(self.context, request_spec,
                                         filter_props)
        select_dest_mock.assert_called_once_with(
            self.context, request_spec, filter_props)
        prep_resize_mock.assert_called_once_with(
            self.context, image, inst_obj, flavor,
            hosts[0]['host'], [resvs],
            request_spec=request_spec,
            filter_properties=filter_props,
            node=hosts[0]['nodename'], clean_shutdown=True)
        notify_mock.assert_called_once_with(self.context, inst_obj.uuid,
                                            'migrate_server', updates,
                                            exc_info, request_spec)
        rollback_mock.assert_called_once_with()

    def test_resize_no_valid_host_error_msg(self):
        flavor = flavors.get_flavor_by_name('m1.tiny')
        flavor_new = flavors.get_flavor_by_name('m1.small')
        inst_obj = objects.Instance(
            image_ref='fake-image_ref',
            vm_state=vm_states.STOPPED,
            instance_type_id=flavor['id'],
            system_metadata={},
            uuid='fake',
            user_id='fake')

        request_spec = dict(instance_type=dict(extra_specs=dict()),
                            instance_properties=dict())
        filter_props = dict(context=None)
        resvs = 'fake-resvs'
        image = 'fake-image'

        with contextlib.nested(
            mock.patch.object(utils, 'get_image_from_system_metadata',
                              return_value=image),
            mock.patch.object(scheduler_utils, 'build_request_spec',
                              return_value=request_spec),
            mock.patch.object(self.conductor, '_set_vm_state_and_notify'),
            mock.patch.object(migrate.MigrationTask,
                              'execute',
                              side_effect=exc.NoValidHost(reason="")),
            mock.patch.object(migrate.MigrationTask, 'rollback')
        ) as (image_mock, brs_mock, vm_st_mock, task_execute_mock,
              task_rb_mock):
            nvh = self.assertRaises(exc.NoValidHost,
                                    self.conductor._cold_migrate, self.context,
                                    inst_obj, flavor_new, filter_props,
                                    [resvs], clean_shutdown=True)
            self.assertIn('resize', nvh.message)

    def test_build_instances_instance_not_found(self):
        instances = [fake_instance.fake_instance_obj(self.context)
                for i in range(2)]
        self.mox.StubOutWithMock(instances[0], 'refresh')
        self.mox.StubOutWithMock(instances[1], 'refresh')
        image = {'fake-data': 'should_pass_silently'}
        spec = {'fake': 'specs',
                'instance_properties': instances[0]}
        self.mox.StubOutWithMock(scheduler_utils, 'build_request_spec')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.conductor_manager.scheduler_client,
                'select_destinations')
        self.mox.StubOutWithMock(self.conductor_manager.compute_rpcapi,
                'build_and_run_instance')

        scheduler_utils.build_request_spec(self.context, image,
                mox.IgnoreArg()).AndReturn(spec)
        scheduler_utils.setup_instance_group(self.context, spec, {})
        self.conductor_manager.scheduler_client.select_destinations(
                self.context, spec,
                {'retry': {'num_attempts': 1, 'hosts': []}}).AndReturn(
                        [{'host': 'host1', 'nodename': 'node1', 'limits': []},
                         {'host': 'host2', 'nodename': 'node2', 'limits': []}])
        instances[0].refresh().AndRaise(
                exc.InstanceNotFound(instance_id=instances[0].uuid))
        instances[1].refresh()
        self.conductor_manager.compute_rpcapi.build_and_run_instance(
                self.context, instance=instances[1], host='host2',
                image={'fake-data': 'should_pass_silently'}, request_spec=spec,
                filter_properties={'limits': [],
                                   'retry': {'num_attempts': 1,
                                             'hosts': [['host2',
                                                        'node2']]}},
                admin_password='admin_password',
                injected_files='injected_files',
                requested_networks=None,
                security_groups='security_groups',
                block_device_mapping=mox.IsA(objects.BlockDeviceMappingList),
                node='node2', limits=[])
        self.mox.ReplayAll()

        # build_instances() is a cast, we need to wait for it to complete
        self.useFixture(cast_as_call.CastAsCall(self.stubs))

        self.conductor.build_instances(self.context,
                instances=instances,
                image=image,
                filter_properties={},
                admin_password='admin_password',
                injected_files='injected_files',
                requested_networks=None,
                security_groups='security_groups',
                block_device_mapping='block_device_mapping',
                legacy_bdm=False)

    @mock.patch.object(scheduler_utils, 'setup_instance_group')
    @mock.patch.object(scheduler_utils, 'build_request_spec')
    def test_build_instances_info_cache_not_found(self, build_request_spec,
                                                  setup_instance_group):
        instances = [fake_instance.fake_instance_obj(self.context)
                for i in range(2)]
        image = {'fake-data': 'should_pass_silently'}
        destinations = [{'host': 'host1', 'nodename': 'node1', 'limits': []},
                {'host': 'host2', 'nodename': 'node2', 'limits': []}]
        spec = {'fake': 'specs',
                'instance_properties': instances[0]}
        build_request_spec.return_value = spec
        with contextlib.nested(
                mock.patch.object(instances[0], 'refresh',
                    side_effect=exc.InstanceInfoCacheNotFound(
                        instance_uuid=instances[0].uuid)),
                mock.patch.object(instances[1], 'refresh'),
                mock.patch.object(self.conductor_manager.scheduler_client,
                    'select_destinations', return_value=destinations),
                mock.patch.object(self.conductor_manager.compute_rpcapi,
                    'build_and_run_instance')
                ) as (inst1_refresh, inst2_refresh, select_destinations,
                        build_and_run_instance):

            # build_instances() is a cast, we need to wait for it to complete
            self.useFixture(cast_as_call.CastAsCall(self.stubs))

            self.conductor.build_instances(self.context,
                    instances=instances,
                    image=image,
                    filter_properties={},
                    admin_password='admin_password',
                    injected_files='injected_files',
                    requested_networks=None,
                    security_groups='security_groups',
                    block_device_mapping='block_device_mapping',
                    legacy_bdm=False)

            # NOTE(sbauza): Due to populate_retry() later in the code,
            # filter_properties is dynamically modified
            setup_instance_group.assert_called_once_with(
                self.context, spec, {'retry': {'num_attempts': 1,
                                               'hosts': []}})
            build_and_run_instance.assert_called_once_with(self.context,
                    instance=instances[1], host='host2', image={'fake-data':
                        'should_pass_silently'}, request_spec=spec,
                    filter_properties={'limits': [],
                                       'retry': {'num_attempts': 1,
                                                 'hosts': [['host2',
                                                            'node2']]}},
                    admin_password='admin_password',
                    injected_files='injected_files',
                    requested_networks=None,
                    security_groups='security_groups',
                    block_device_mapping=mock.ANY,
                    node='node2', limits=[])


class ConductorTaskRPCAPITestCase(_BaseTaskTestCase,
        test_compute.BaseTestCase):
    """Conductor compute_task RPC namespace Tests."""
    def setUp(self):
        super(ConductorTaskRPCAPITestCase, self).setUp()
        self.conductor_service = self.start_service(
            'conductor', manager='nova.conductor.manager.ConductorManager')
        self.conductor = conductor_rpcapi.ComputeTaskAPI()
        service_manager = self.conductor_service.manager
        self.conductor_manager = service_manager.compute_task_mgr


class ConductorTaskAPITestCase(_BaseTaskTestCase, test_compute.BaseTestCase):
    """Compute task API Tests."""
    def setUp(self):
        super(ConductorTaskAPITestCase, self).setUp()
        self.conductor_service = self.start_service(
            'conductor', manager='nova.conductor.manager.ConductorManager')
        self.conductor = conductor_api.ComputeTaskAPI()
        service_manager = self.conductor_service.manager
        self.conductor_manager = service_manager.compute_task_mgr


class ConductorLocalComputeTaskAPITestCase(ConductorTaskAPITestCase):
    """Conductor LocalComputeTaskAPI Tests."""
    def setUp(self):
        super(ConductorLocalComputeTaskAPITestCase, self).setUp()
        self.conductor = conductor_api.LocalComputeTaskAPI()
        self.conductor_manager = self.conductor._manager._target


class ConductorV3ManagerProxyTestCase(test.NoDBTestCase):
    def test_v3_manager_proxy(self):
        manager = conductor_manager.ConductorManager()
        proxy = conductor_manager._ConductorManagerV3Proxy(manager)
        ctxt = context.get_admin_context()

        methods = [
            # (method, number_of_args)
            ('provider_fw_rule_get_all', 0),
            ('object_class_action_versions', 5),
            ('object_action', 4),
            ('object_backport_versions', 2),
        ]

        for method, num_args in methods:
            args = range(num_args)
            with mock.patch.object(manager, method) as mock_method:
                getattr(proxy, method)(ctxt, *args)
                mock_method.assert_called_once_with(ctxt, *args)
