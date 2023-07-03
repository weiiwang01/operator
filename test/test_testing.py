# Copyright 2019-2020 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import datetime
import grp
import importlib
import inspect
import io
import ipaddress
import os
import pathlib
import platform
import pwd
import shutil
import sys
import tempfile
import textwrap
import unittest
import uuid
from unittest.mock import MagicMock

import pytest
import yaml

import ops
import ops.testing
from ops import pebble
from ops.model import _ModelBackend
from ops.testing import _TestingPebbleClient

is_linux = platform.system() == 'Linux'


class SetLeaderErrorTester(ops.CharmBase):
    """Sets peer relation data inside leader-elected."""

    def __init__(self, framework):
        super().__init__(framework)
        self._peer_name = 'peer'
        self.framework.observe(self.on.leader_elected,
                               self._on_leader_elected)

    def _on_leader_elected(self, event):
        peers = self.model.get_relation(self._peer_name)
        peers.data[self.app]["foo"] = "bar"


class StorageTester(ops.CharmBase):
    """Record the relation-changed events."""

    def __init__(self, framework):
        super().__init__(framework)
        self.observed_events = []
        self.framework.observe(self.on.test_storage_attached,
                               self._on_test_storage_attached)
        self.framework.observe(self.on.test_storage_detaching,
                               self._on_test_storage_detaching)

    def _on_test_storage_attached(self, event):
        self.observed_events.append(event)

    def _on_test_storage_detaching(self, event):
        self.observed_events.append(event)


class StorageWithHyphensHelper(ops.Object):
    def __init__(self, parent, key):
        super().__init__(parent, key)
        self.changes = []
        parent.framework.observe(parent.on.test_with_hyphens_storage_attached,
                                 self.on_storage_changed)
        parent.framework.observe(parent.on.test_with_hyphens_storage_detaching,
                                 self.on_storage_changed)

    def on_storage_changed(self, event):
        self.changes.append(event)


class TestHarness(unittest.TestCase):

    def test_add_relation(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id, int)
        backend = harness._backend
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id), [])
        # Make sure the initial data bags for our app and unit are empty.
        self.assertEqual(backend.relation_get(rel_id, 'test-app', is_app=True), {})
        self.assertEqual(backend.relation_get(rel_id, 'test-app/0', is_app=False), {})

    def test_can_connect_default(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            containers:
              foo:
                resource: foo-image
            ''')
        self.addCleanup(harness.cleanup)

        harness.begin()
        c = harness.model.unit.get_container('foo')

        self.assertFalse(c.can_connect())
        with self.assertRaises(pebble.ConnectionError):
            c.get_plan()

        harness.set_can_connect('foo', True)
        self.assertTrue(c.can_connect())

        harness.set_can_connect('foo', False)
        self.assertFalse(c.can_connect())

        harness.container_pebble_ready('foo')
        self.assertTrue(c.can_connect())
        c.get_plan()  # shouldn't raise ConnectionError

    def test_can_connect_begin_with_initial_hooks(self):
        pebble_ready_calls = collections.defaultdict(int)

        class MyCharm(ops.CharmBase):
            def __init__(self, *args):
                super().__init__(*args)
                self.framework.observe(self.on.foo_pebble_ready, self._on_pebble_ready)
                self.framework.observe(self.on.bar_pebble_ready, self._on_pebble_ready)

            def _on_pebble_ready(self, event: ops.PebbleReadyEvent):
                assert event.workload.can_connect()
                pebble_ready_calls[event.workload.name] += 1

        harness = ops.testing.Harness(MyCharm, meta='''
            name: test-app
            containers:
              foo:
                resource: foo-image
              bar:
                resource: bar-image
            ''')
        self.addCleanup(harness.cleanup)

        harness.begin_with_initial_hooks()
        self.assertEqual(dict(pebble_ready_calls), {'foo': 1, 'bar': 1})
        self.assertTrue(harness.model.unit.containers['foo'].can_connect())
        self.assertTrue(harness.model.unit.containers['bar'].can_connect())

        harness.set_can_connect('foo', False)
        self.assertFalse(harness.model.unit.containers['foo'].can_connect())

        harness.set_can_connect('foo', True)
        container = harness.model.unit.containers['foo']
        self.assertTrue(container.can_connect())
        container.get_plan()  # shouldn't raise ConnectionError

    def test_add_relation_and_unit(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id, int)
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'foo': 'bar'})
        backend = harness._backend
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id), ['postgresql/0'])
        self.assertEqual(
            backend.relation_get(rel_id, 'postgresql/0', is_app=False),
            {'foo': 'bar'})

    def test_add_relation_with_remote_app_data(self):
        # language=YAML
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        remote_app = 'postgresql'
        rel_id = harness.add_relation('db', remote_app)
        harness.update_relation_data(rel_id, 'postgresql', {'app': 'data'})
        self.assertIsInstance(rel_id, int)
        backend = harness._backend
        self.assertEqual([rel_id], backend.relation_ids('db'))
        self.assertEqual({'app': 'data'}, backend.relation_get(rel_id, remote_app, is_app=True))

    def test_add_relation_with_our_initial_data(self):

        class InitialDataTester(ops.CharmBase):
            """Record the relation-changed events."""

            def __init__(self, framework):
                super().__init__(framework)
                self.observed_events = []
                self.framework.observe(self.on.db_relation_changed, self._on_db_relation_changed)

            def _on_db_relation_changed(self, event):
                self.observed_events.append(event)

        # language=YAML
        harness = ops.testing.Harness(InitialDataTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        rel_id = harness.add_relation('db', 'postgresql')
        harness.update_relation_data(rel_id, 'test-app', {'k': 'v1'})
        harness.update_relation_data(rel_id, 'test-app/0', {'ingress-address': '192.0.2.1'})
        backend = harness._backend
        self.assertEqual({'k': 'v1'}, backend.relation_get(rel_id, 'test-app', is_app=True))
        self.assertEqual({'ingress-address': '192.0.2.1'},
                         backend.relation_get(rel_id, 'test-app/0', is_app=False))

        harness.begin()
        self.assertEqual({'k': 'v1'}, backend.relation_get(rel_id, 'test-app', is_app=True))
        self.assertEqual({'ingress-address': '192.0.2.1'},
                         backend.relation_get(rel_id, 'test-app/0', is_app=False))
        # Make sure no relation-changed events are emitted for our own data bags.
        self.assertEqual([], harness.charm.observed_events)

        # A remote unit can still update our app relation data bag since our unit is not a leader.
        harness.update_relation_data(rel_id, 'test-app', {'k': 'v2'})
        # And we get an event
        self.assertEqual([], harness.charm.observed_events)
        # We can also update our own relation data, even if it is a bit 'cheaty'
        harness.update_relation_data(rel_id, 'test-app/0', {'ingress-address': '192.0.2.2'})
        # But no event happens

        # Updating our data app relation data bag and our unit data bag does not generate events.
        harness.set_leader(True)
        harness.update_relation_data(rel_id, 'test-app', {'k': 'v3'})
        harness.update_relation_data(rel_id, 'test-app/0', {'ingress-address': '192.0.2.2'})
        self.assertEqual([], harness.charm.observed_events)

    def test_add_peer_relation_with_initial_data_leader(self):

        class InitialDataTester(ops.CharmBase):
            """Record the relation-changed events."""

            def __init__(self, framework):
                super().__init__(framework)
                self.observed_events = []
                self.framework.observe(self.on.cluster_relation_changed,
                                       self._on_cluster_relation_changed)

            def _on_cluster_relation_changed(self, event):
                self.observed_events.append(event)

        # language=YAML
        harness = ops.testing.Harness(InitialDataTester, meta='''
            name: test-app
            peers:
                cluster:
                    interface: cluster
            ''')
        self.addCleanup(harness.cleanup)
        # TODO: dmitriis 2020-04-07 test a minion unit and initial peer relation app data
        # events when the harness begins to emit events for initial data.
        harness.set_leader(is_leader=True)
        rel_id = harness.add_relation('cluster', 'test-app')
        harness.update_relation_data(rel_id, 'test-app', {'k': 'v'})
        harness.update_relation_data(rel_id, 'test-app/0', {'ingress-address': '192.0.2.1'})
        backend = harness._backend
        self.assertEqual({'k': 'v'}, backend.relation_get(rel_id, 'test-app', is_app=True))
        self.assertEqual({'ingress-address': '192.0.2.1'},
                         backend.relation_get(rel_id, 'test-app/0', is_app=False))

        harness.begin()
        self.assertEqual({'k': 'v'}, backend.relation_get(rel_id, 'test-app', is_app=True))
        self.assertEqual({'ingress-address': '192.0.2.1'},
                         backend.relation_get(rel_id, 'test-app/0', is_app=False))
        # Make sure no relation-changed events are emitted for our own data bags.
        self.assertEqual([], harness.charm.observed_events)

        # Updating our app relation data bag and our unit data bag does not trigger events
        harness.update_relation_data(rel_id, 'test-app', {'k': 'v2'})
        harness.update_relation_data(rel_id, 'test-app/0', {'ingress-address': '192.0.2.2'})
        self.assertEqual([], harness.charm.observed_events)

        # If our unit becomes a minion, updating app relation data indirectly becomes possible
        # and our charm gets notifications.
        harness.set_leader(False)
        harness.update_relation_data(rel_id, 'test-app', {'k': 'v3'})
        self.assertEqual({'k': 'v3'}, backend.relation_get(rel_id, 'test-app', is_app=True))
        self.assertTrue(len(harness.charm.observed_events), 1)
        self.assertIsInstance(harness.charm.observed_events[0], ops.RelationEvent)

    def test_relation_get_when_broken(self):
        harness = ops.testing.Harness(RelationBrokenTester, meta='''
            name: test-app
            requires:
                foo:
                    interface: foofoo
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('foo')

        # relation remote app is None to mirror production Juju behavior where Juju doesn't
        # communicate the remote app to ops.
        rel_id = harness.add_relation('foo', None)

        with pytest.raises(KeyError, match='trying to access remote app data'):
            harness.remove_relation(rel_id)

    def test_remove_relation(self):
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        # First create a relation
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id, int)
        harness.add_relation_unit(rel_id, 'postgresql/0')
        backend = harness._backend
        # Check relation was created
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id), ['postgresql/0'])
        harness.charm.get_changes(reset=True)  # created event ignored
        # Now remove relation
        harness.remove_relation(rel_id)
        # Check relation no longer exists
        self.assertEqual(backend.relation_ids('db'), [])
        self.assertRaises(ops.RelationNotFoundError, backend.relation_list, rel_id)
        # Check relation broken event is raised with correct data
        changes = harness.charm.get_changes()
        self.assertEqual(changes[0],
                         {'name': 'relation-departed',
                          'relation': 'db',
                          'data': {'app': 'postgresql',
                                   'unit': 'postgresql/0',
                                   'departing_unit': 'postgresql/0',
                                   'relation_id': 0}})
        self.assertEqual(changes[1],
                         {'name': 'relation-broken',
                          'relation': 'db',
                          'data': {'app': 'postgresql',
                                   'unit': None,
                                   'relation_id': rel_id}})

    def test_remove_specific_relation_id(self):
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')

        # Create the first relation
        rel_id_1 = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id_1, int)
        harness.add_relation_unit(rel_id_1, 'postgresql/0')
        backend = harness._backend
        # Check relation was created
        self.assertIn(rel_id_1, backend.relation_ids('db'))
        self.assertEqual(backend.relation_list(rel_id_1), ['postgresql/0'])
        harness.charm.get_changes(reset=True)  # created event ignored

        # Create the second relation
        rel_id_2 = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id_2, int)
        harness.add_relation_unit(rel_id_2, 'postgresql/1')
        backend = harness._backend
        # Check relation was created and both relations exist
        self.assertIn(rel_id_1, backend.relation_ids('db'))
        self.assertIn(rel_id_2, backend.relation_ids('db'))
        self.assertEqual(backend.relation_list(rel_id_2), ['postgresql/1'])
        harness.charm.get_changes(reset=True)  # created event ignored

        # Now remove second relation
        harness.remove_relation(rel_id_2)
        # Check second relation no longer exists but first does
        self.assertEqual(backend.relation_ids('db'), [rel_id_1])
        self.assertRaises(ops.RelationNotFoundError, backend.relation_list, rel_id_2)

        # Check relation broken event is raised with correct data
        changes = harness.charm.get_changes()
        self.assertEqual(changes[0],
                         {'name': 'relation-departed',
                          'relation': 'db',
                          'data': {'app': 'postgresql',
                                   'unit': 'postgresql/1',
                                   'departing_unit': 'postgresql/1',
                                   'relation_id': rel_id_2}})
        self.assertEqual(changes[1],
                         {'name': 'relation-broken',
                          'relation': 'db',
                          'data': {'app': 'postgresql',
                                   'unit': None,
                                   'relation_id': rel_id_2}})

    def test_removing_invalid_relation_id_raises_exception(self):
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        # First create a relation
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id, int)
        harness.add_relation_unit(rel_id, 'postgresql/0')
        backend = harness._backend
        # Check relation was created
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id), ['postgresql/0'])
        harness.charm.get_changes(reset=True)  # created event ignored
        # Check exception is raised if relation id is invalid
        with self.assertRaises(ops.RelationNotFoundError):
            harness.remove_relation(rel_id + 1)

    def test_remove_relation_unit(self):
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        # First add a relation and unit
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id, int)
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'foo': 'bar'})
        # Check relation and unit were created
        backend = harness._backend
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id), ['postgresql/0'])
        harness.charm.get_changes(reset=True)  # ignore relation created events
        relation = harness.charm.model.get_relation('db')
        self.assertEqual(len(relation.units), 1)
        # Check relation data is correct
        rel_unit = harness.charm.model.get_unit('postgresql/0')
        self.assertEqual(relation.data[rel_unit]['foo'], 'bar')
        # Instruct the charm to record the relation data it sees in the list of changes
        harness.charm.record_relation_data_on_events = True
        # Now remove unit
        harness.remove_relation_unit(rel_id, 'postgresql/0')
        # Check relation still exists
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        # Check removed unit does not exist
        self.assertEqual(backend.relation_list(rel_id), [])
        # Check the unit is actually removed from the relations the model knows about
        self.assertEqual(len(harness.charm.model.get_relation('db').units), 0)
        self.assertFalse(rel_unit in harness.charm.model.get_relation('db').data)
        # Check relation departed was raised with correct data
        self.assertEqual({'name': 'relation-departed',
                          'relation': 'db',
                          'data': {'app': 'postgresql',
                                   'unit': 'postgresql/0',
                                   'departing_unit': 'postgresql/0',
                                   'relation_id': 0,
                                   'relation_data': {'test-app/0': {},
                                                     'test-app': {},
                                                     'postgresql/0': {'foo': 'bar'},
                                                     'postgresql': {}}}},
                         harness.charm.get_changes()[0])

    def test_removing_relation_removes_remote_app_data(self):
        # language=YAML
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        # Add a relation and update app data
        remote_app = 'postgresql'
        rel_id = harness.add_relation('db', remote_app)
        harness.update_relation_data(rel_id, 'postgresql', {'app': 'data'})
        self.assertIsInstance(rel_id, int)
        # Check relation app data exists
        backend = harness._backend
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual({'app': 'data'}, backend.relation_get(rel_id, remote_app, is_app=True))
        harness.remove_relation(rel_id)
        # Check relation and app data are removed
        self.assertEqual(backend.relation_ids('db'), [])
        with harness._event_context('foo'):
            self.assertRaises(ops.RelationNotFoundError, backend.relation_get,
                              rel_id, remote_app, is_app=True)

    def test_removing_relation_refreshes_charm_model(self):
        # language=YAML
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        # Add a relation and update app data
        remote_app = 'postgresql'
        rel_id = harness.add_relation('db', remote_app)
        harness.update_relation_data(rel_id, 'postgresql', {'app': 'data'})
        self.assertIsInstance(rel_id, int)
        self.assertIsNotNone(self._find_relation_in_model_by_id(harness, rel_id))

        # Check relation app data exists
        backend = harness._backend
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual({'app': 'data'}, backend.relation_get(rel_id, remote_app, is_app=True))
        harness.remove_relation(rel_id)
        self.assertIsNone(self._find_relation_in_model_by_id(harness, rel_id))

    def _find_relation_in_model_by_id(self, harness, rel_id):
        for relations in harness.charm.model.relations.values():
            for relation in relations:
                if rel_id == relation.id:
                    return relation
        return None

    def test_removing_relation_unit_removes_data_also(self):
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        # Add a relation and unit with data
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id, int)
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'foo': 'bar'})
        # Check relation, unit and data exist
        backend = harness._backend
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id), ['postgresql/0'])
        self.assertEqual(
            backend.relation_get(rel_id, 'postgresql/0', is_app=False),
            {'foo': 'bar'})
        harness.charm.get_changes(reset=True)  # ignore relation created events
        # Remove unit but not relation
        harness.remove_relation_unit(rel_id, 'postgresql/0')
        # Check relation exists but unit and data are removed
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id), [])
        self.assertRaises(KeyError,
                          backend.relation_get,
                          rel_id,
                          'postgresql/0',
                          is_app=False)
        # Check relation departed was raised with correct data
        self.assertEqual(harness.charm.get_changes()[0],
                         {'name': 'relation-departed',
                          'relation': 'db',
                          'data': {'app': 'postgresql',
                                   'unit': 'postgresql/0',
                                   'departing_unit': 'postgresql/0',
                                   'relation_id': rel_id}})

    def test_removing_relation_unit_does_not_remove_other_unit_and_data(self):
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        # Add a relation with two units with data
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertIsInstance(rel_id, int)
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.add_relation_unit(rel_id, 'postgresql/1')
        harness.update_relation_data(rel_id, 'postgresql/0', {'foo0': 'bar0'})
        harness.update_relation_data(rel_id, 'postgresql/1', {'foo1': 'bar1'})
        # Check both unit and data are present
        backend = harness._backend
        self.assertEqual(backend.relation_ids('db'), [rel_id])
        self.assertEqual(backend.relation_list(rel_id),
                         ['postgresql/0', 'postgresql/1'])
        self.assertEqual(
            backend.relation_get(rel_id, 'postgresql/0', is_app=False),
            {'foo0': 'bar0'})
        self.assertEqual(
            backend.relation_get(rel_id, 'postgresql/1', is_app=False),
            {'foo1': 'bar1'})
        harness.charm.get_changes(reset=True)  # ignore relation created events
        # Remove only one unit
        harness.remove_relation_unit(rel_id, 'postgresql/1')
        # Check other unit and data still exists
        self.assertEqual(backend.relation_list(rel_id),
                         ['postgresql/0'])
        self.assertEqual(
            backend.relation_get(rel_id, 'postgresql/0', is_app=False),
            {'foo0': 'bar0'})
        # Check relation departed was raised with correct data
        self.assertEqual(harness.charm.get_changes()[0],
                         {'name': 'relation-departed',
                          'relation': 'db',
                          'data': {'app': 'postgresql',
                                   'unit': 'postgresql/1',
                                   'departing_unit': 'postgresql/1',
                                   'relation_id': rel_id}})

    def test_relation_events(self):
        harness = ops.testing.Harness(RelationEventCharm, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
        ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.charm.observe_relation_events('db')
        self.assertEqual(harness.charm.get_changes(), [])
        rel_id = harness.add_relation('db', 'postgresql')
        self.assertEqual(
            harness.charm.get_changes(),
            [{'name': 'relation-created',
              'relation': 'db',
              'data': {
                  'app': 'postgresql',
                  'unit': None,
                  'relation_id': rel_id,
              }}])
        harness.add_relation_unit(rel_id, 'postgresql/0')
        self.assertEqual(
            harness.charm.get_changes(),
            [{'name': 'relation-joined',
              'relation': 'db',
              'data': {
                  'app': 'postgresql',
                  'unit': 'postgresql/0',
                  'relation_id': rel_id,
              }}])
        harness.update_relation_data(rel_id, 'postgresql', {'foo': 'bar'})
        self.assertEqual(
            harness.charm.get_changes(),
            [{'name': 'relation-changed',
              'relation': 'db',
              'data': {
                  'app': 'postgresql',
                  'unit': None,
                  'relation_id': rel_id,
              }}])
        harness.update_relation_data(rel_id, 'postgresql/0', {'baz': 'bing'})
        self.assertEqual(
            harness.charm.get_changes(),
            [{'name': 'relation-changed',
              'relation': 'db',
              'data': {
                  'app': 'postgresql',
                  'unit': 'postgresql/0',
                  'relation_id': rel_id,
              }}])

    def test_get_relation_data(self):
        charm_meta = '''
            name: test-app
            requires:
                db:
                    interface: pgsql
        '''
        harness = ops.testing.Harness(ops.CharmBase, meta=charm_meta)
        self.addCleanup(harness.cleanup)
        rel_id = harness.add_relation('db', 'postgresql')
        harness.update_relation_data(rel_id, 'postgresql', {'remote': 'data'})
        self.assertEqual(harness.get_relation_data(rel_id, 'test-app'), {})
        self.assertEqual(harness.get_relation_data(rel_id, 'test-app/0'), {})
        self.assertEqual(harness.get_relation_data(rel_id, 'test-app/1'), None)
        self.assertEqual(harness.get_relation_data(rel_id, 'postgresql'), {'remote': 'data'})
        with self.assertRaises(KeyError):
            # unknown relation id
            harness.get_relation_data(99, 'postgresql')

        meta = yaml.safe_load(charm_meta)
        t_app = ops.Application('test-app', meta, harness._backend, None)
        t_unit0 = ops.Unit('test-app/0', meta, harness._backend, {ops.Application: t_app})
        t_unit1 = ops.Unit('test-app/1', meta, harness._backend, {ops.Application: t_app})
        self.assertEqual(harness.get_relation_data(rel_id, t_app), {})
        self.assertEqual(harness.get_relation_data(rel_id, t_unit0), {})
        self.assertEqual(harness.get_relation_data(rel_id, t_unit1), None)
        pg_app = ops.Application('postgresql', meta, harness._backend, None)
        self.assertEqual(harness.get_relation_data(rel_id, pg_app), {'remote': 'data'})

    def test_create_harness_twice(self):
        metadata = '''
            name: my-charm
            requires:
              db:
                interface: pgsql
            '''
        harness1 = ops.testing.Harness(ops.CharmBase, meta=metadata)
        self.addCleanup(harness1.cleanup)
        harness2 = ops.testing.Harness(ops.CharmBase, meta=metadata)
        self.addCleanup(harness2.cleanup)
        harness1.begin()
        harness2.begin()
        helper1 = DBRelationChangedHelper(harness1.charm, "helper1")
        helper2 = DBRelationChangedHelper(harness2.charm, "helper2")
        rel_id = harness2.add_relation('db', 'postgresql')
        harness2.update_relation_data(rel_id, 'postgresql', {'key': 'value'})
        # Helper2 should see the event triggered by harness2, but helper1 should see no events.
        self.assertEqual(helper1.changes, [])
        self.assertEqual(helper2.changes, [(rel_id, 'postgresql')])

    def test_begin_twice(self):
        # language=YAML
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        with self.assertRaises(RuntimeError):
            harness.begin()

    def test_update_relation_exposes_new_data(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        viewer = RelationChangedViewer(harness.charm, 'db')
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'initial': 'data'})
        self.assertEqual(viewer.changes, [{'initial': 'data'}])
        harness.update_relation_data(rel_id, 'postgresql/0', {'new': 'value'})
        self.assertEqual(viewer.changes, [{'initial': 'data'},
                                          {'initial': 'data', 'new': 'value'}])

    def test_update_relation_no_local_unit_change_event(self):
        # language=YAML
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        helper = DBRelationChangedHelper(harness.charm, "helper")
        rel_id = harness.add_relation('db', 'postgresql')
        rel = harness.charm.model.get_relation('db')
        rel.data[harness.charm.model.unit]['key'] = 'value'
        # there should be no event for updating our own data
        harness.update_relation_data(rel_id, 'my-charm/0', {'new': 'other'})
        # but the data will be updated.
        self.assertEqual({'key': 'value', 'new': 'other'}, rel.data[harness.charm.model.unit])

        rel.data[harness.charm.model.unit]['new'] = 'value'
        # Our unit data bag got updated.
        self.assertEqual(rel.data[harness.charm.model.unit]['new'], 'value')
        # But there were no changed events registered by our unit.
        self.assertEqual([], helper.changes)

    def test_update_peer_relation_no_local_unit_change_event(self):
        # language=YAML
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: postgresql
            peers:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        helper = DBRelationChangedHelper(harness.charm, "helper")
        rel_id = harness.add_relation('db', 'postgresql')

        rel = harness.charm.model.get_relation('db')
        rel.data[harness.charm.model.unit]['key'] = 'value'
        rel = harness.charm.model.get_relation('db')
        harness.update_relation_data(rel_id, 'postgresql/0', {'key': 'v1'})
        self.assertEqual({'key': 'v1'}, rel.data[harness.charm.model.unit])
        # Make sure there was no event
        self.assertEqual([], helper.changes)

        rel.data[harness.charm.model.unit]['key'] = 'v2'
        # Our unit data bag got updated.
        self.assertEqual({'key': 'v2'}, dict(rel.data[harness.charm.model.unit]))
        # But there were no changed events registered by our unit.
        self.assertEqual([], helper.changes)

        # Same for when our unit is a leader.
        harness.set_leader(is_leader=True)
        harness.update_relation_data(rel_id, 'postgresql/0', {'key': 'v3'})
        self.assertEqual({'key': 'v3'}, dict(rel.data[harness.charm.model.unit]))
        self.assertEqual([], helper.changes)

        rel.data[harness.charm.model.unit]['key'] = 'v4'
        self.assertEqual(rel.data[harness.charm.model.unit]['key'], 'v4')
        self.assertEqual([], helper.changes)

    def test_harness_leader_misconfig(self):
        # language=YAML
        harness = ops.testing.Harness(SetLeaderErrorTester, meta='''
            name: postgresql
            peers:
              peer:
                interface: foo
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()

        with self.assertRaises(RuntimeError) as cm:
            harness.set_leader(is_leader=True)
        self.assertTrue(cm.exception.args[0].find('use Harness.add_relation') != -1)

    def test_update_peer_relation_app_data(self):
        # language=YAML
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: postgresql
            peers:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_leader(is_leader=True)
        helper = DBRelationChangedHelper(harness.charm, "helper")
        rel_id = harness.add_relation('db', 'postgresql')
        rel = harness.charm.model.get_relation('db')
        rel.data[harness.charm.app]['key'] = 'value'
        harness.update_relation_data(rel_id, 'postgresql', {'key': 'v1'})
        self.assertEqual({'key': 'v1'}, rel.data[harness.charm.app])
        self.assertEqual([], helper.changes)

        rel.data[harness.charm.app]['key'] = 'v2'
        # Our unit data bag got updated.
        self.assertEqual(rel.data[harness.charm.model.app]['key'], 'v2')
        # But there were no changed events registered by our unit.
        self.assertEqual([], helper.changes)

        # If our unit is not a leader unit we get an update about peer app relation data changes.
        harness.set_leader(is_leader=False)
        harness.update_relation_data(rel_id, 'postgresql', {'k2': 'v2'})
        self.assertEqual(rel.data[harness.charm.model.app]['k2'], 'v2')
        self.assertEqual(helper.changes, [(0, 'postgresql')])

    def test_update_relation_no_local_app_change_event(self):
        # language=YAML
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_leader(False)
        helper = DBRelationChangedHelper(harness.charm, "helper")
        rel_id = harness.add_relation('db', 'postgresql')
        # TODO: remove this as soon as https://github.com/canonical/operator/issues/175 is fixed.
        harness.add_relation_unit(rel_id, 'postgresql/0')
        self.assertEqual(helper.changes, [])

        harness.update_relation_data(rel_id, 'my-charm', {'new': 'value'})
        rel = harness.charm.model.get_relation('db')
        self.assertEqual(rel.data[harness.charm.app]['new'], 'value')

        # Our app data bag got updated.
        self.assertEqual(rel.data[harness.charm.model.app]['new'], 'value')
        # But there were no changed events registered by our unit.
        self.assertEqual(helper.changes, [])

    def test_update_relation_remove_data(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        viewer = RelationChangedViewer(harness.charm, 'db')
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'initial': 'data'})
        harness.update_relation_data(rel_id, 'postgresql/0', {'initial': ''})
        self.assertEqual(viewer.changes, [{'initial': 'data'}, {}])

    def test_no_event_on_empty_update_relation_unit_app(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        viewer = RelationChangedViewer(harness.charm, 'db')
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql', {'initial': 'data'})
        harness.update_relation_data(rel_id, 'postgresql', {})
        self.assertEqual(viewer.changes, [{'initial': 'data'}])

    def test_no_event_on_no_diff_update_relation_unit_app(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        viewer = RelationChangedViewer(harness.charm, 'db')
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql', {'initial': 'data'})
        harness.update_relation_data(rel_id, 'postgresql', {'initial': 'data'})
        self.assertEqual(viewer.changes, [{'initial': 'data'}])

    def test_no_event_on_empty_update_relation_unit_bag(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        viewer = RelationChangedViewer(harness.charm, 'db')
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'initial': 'data'})
        harness.update_relation_data(rel_id, 'postgresql/0', {})
        self.assertEqual(viewer.changes, [{'initial': 'data'}])

    def test_no_event_on_no_diff_update_relation_unit_bag(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: my-charm
            requires:
              db:
                interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        viewer = RelationChangedViewer(harness.charm, 'db')
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'initial': 'data'})
        harness.update_relation_data(rel_id, 'postgresql/0', {'initial': 'data'})
        self.assertEqual(viewer.changes, [{'initial': 'data'}])

    def test_empty_config_raises(self):
        with self.assertRaises(TypeError):
            ops.testing.Harness(RecordingCharm, config='')

    def test_update_config(self):
        harness = ops.testing.Harness(RecordingCharm, config='''
            options:
                a:
                    description: a config option
                    type: string
                b:
                    description: another config option
                    type: int
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.update_config(key_values={'a': 'foo', 'b': 2})
        self.assertEqual(
            harness.charm.changes,
            [{'name': 'config-changed', 'data': {'a': 'foo', 'b': 2}}])
        harness.update_config(key_values={'b': 3})
        self.assertEqual(
            harness.charm.changes,
            [{'name': 'config-changed', 'data': {'a': 'foo', 'b': 2}},
             {'name': 'config-changed', 'data': {'a': 'foo', 'b': 3}}])
        # you can set config values to the empty string, you can use unset to actually remove items
        harness.update_config(key_values={'a': ''}, unset=set('b'))
        self.assertEqual(
            harness.charm.changes,
            [{'name': 'config-changed', 'data': {'a': 'foo', 'b': 2}},
             {'name': 'config-changed', 'data': {'a': 'foo', 'b': 3}},
             {'name': 'config-changed', 'data': {'a': ''}},
             ])

    def test_update_config_undefined_option(self):
        harness = ops.testing.Harness(RecordingCharm)
        self.addCleanup(harness.cleanup)
        harness.begin()
        with self.assertRaises(ValueError):
            harness.update_config(key_values={'nonexistent': 'foo'})

    def test_update_config_bad_type(self):
        harness = ops.testing.Harness(RecordingCharm, config='''
            options:
                a:
                    description: a config option
                    type: boolean
                    default: false
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        with self.assertRaises(RuntimeError):
            # cannot cast to bool
            harness.update_config(key_values={'a': 'foo'})

        with self.assertRaises(RuntimeError):
            # cannot cast to float
            harness.update_config(key_values={'a': 42.42})

        with self.assertRaises(RuntimeError):
            # cannot cast to int
            harness.update_config(key_values={'a': 42})

        # can cast to bool!
        harness.update_config(key_values={'a': False})

    def test_bad_config_option_type(self):
        with self.assertRaises(RuntimeError):
            ops.testing.Harness(RecordingCharm, config='''
                options:
                    a:
                        description: a config option
                        type: gibberish
                        default: False
                ''')

    def test_no_config_option_type(self):
        with self.assertRaises(RuntimeError):
            ops.testing.Harness(RecordingCharm, config='''
                options:
                    a:
                        description: a config option
                        default: False
                ''')

    def test_uncastable_config_option_type(self):
        with self.assertRaises(RuntimeError):
            ops.testing.Harness(RecordingCharm, config='''
                options:
                    a:
                        description: a config option
                        type: boolean
                        default: peek-a-bool!
                ''')

    def test_update_config_unset_boolean(self):
        harness = ops.testing.Harness(RecordingCharm, config='''
            options:
                a:
                    description: a config option
                    type: boolean
                    default: False
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        # Check the default was set correctly
        self.assertEqual(harness.charm.config, {'a': False})
        # Set the boolean value to True
        harness.update_config(key_values={'a': True})
        self.assertEqual(harness.charm.changes, [{'name': 'config-changed', 'data': {'a': True}}])
        # Unset the boolean value
        harness.update_config(unset={'a'})
        self.assertEqual(
            harness.charm.changes,
            [{'name': 'config-changed', 'data': {'a': True}},
             {'name': 'config-changed', 'data': {'a': False}}])

    def test_set_leader(self):
        harness = ops.testing.Harness(RecordingCharm)
        self.addCleanup(harness.cleanup)
        # No event happens here
        harness.set_leader(False)
        harness.begin()
        self.assertFalse(harness.charm.model.unit.is_leader())
        harness.set_leader(True)
        self.assertEqual(harness.charm.get_changes(reset=True), [{'name': 'leader-elected'}])
        self.assertTrue(harness.charm.model.unit.is_leader())
        harness.set_leader(False)
        self.assertFalse(harness.charm.model.unit.is_leader())
        # No hook event when you lose leadership.
        # TODO: verify if Juju always triggers `leader-settings-changed` if you
        #   lose leadership.
        self.assertEqual(harness.charm.get_changes(reset=True), [])
        harness.disable_hooks()
        harness.set_leader(True)
        # No hook event if you have disabled them
        self.assertEqual(harness.charm.get_changes(reset=True), [])

    def test_relation_set_app_not_leader(self):
        harness = ops.testing.Harness(RecordingCharm, meta='''
            name: test-charm
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_leader(False)
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        rel = harness.charm.model.get_relation('db')
        with harness._event_context('foo'):
            with self.assertRaises(ops.ModelError):
                rel.data[harness.charm.app]['foo'] = 'bar'
        # The data has not actually been changed
        self.assertEqual(harness.get_relation_data(rel_id, 'test-charm'), {})
        harness.set_leader(True)
        rel.data[harness.charm.app]['foo'] = 'bar'
        self.assertEqual(harness.get_relation_data(rel_id, 'test-charm'), {'foo': 'bar'})

    def test_hooks_enabled_and_disabled(self):
        harness = ops.testing.Harness(
            RecordingCharm,
            meta='''
                    name: test-charm
                ''',
            config='''
                    options:
                        value:
                            type: string
                        third:
                            type: string
                    ''')
        self.addCleanup(harness.cleanup)
        # Before begin() there are no events.
        harness.update_config({'value': 'first'})
        # By default, after begin the charm is set up to receive events.
        harness.begin()
        harness.update_config({'value': 'second'})
        self.assertEqual(
            harness.charm.get_changes(reset=True),
            [{'name': 'config-changed', 'data': {'value': 'second'}}])
        # Once disabled, we won't see config-changed when we make an update
        harness.disable_hooks()
        harness.update_config({'third': '3'})
        self.assertEqual(harness.charm.get_changes(reset=True), [])
        harness.enable_hooks()
        harness.update_config({'value': 'fourth'})
        self.assertEqual(
            harness.charm.get_changes(reset=True),
            [{'name': 'config-changed', 'data': {'value': 'fourth', 'third': '3'}}])

    def test_hooks_disabled_contextmanager(self):
        harness = ops.testing.Harness(RecordingCharm, meta='''
                name: test-charm
                ''', config='''
                options:
                    value:
                        type: string
                    third:
                        type: string
            ''')
        self.addCleanup(harness.cleanup)
        # Before begin() there are no events.
        harness.update_config({'value': 'first'})
        # By default, after begin the charm is set up to receive events.
        harness.begin()
        harness.update_config({'value': 'second'})
        self.assertEqual(
            harness.charm.get_changes(reset=True),
            [{'name': 'config-changed', 'data': {'value': 'second'}}])
        # Once disabled, we won't see config-changed when we make an update
        with harness.hooks_disabled():
            harness.update_config({'third': '3'})
        self.assertEqual(harness.charm.get_changes(reset=True), [])
        harness.update_config({'value': 'fourth'})
        self.assertEqual(
            harness.charm.get_changes(reset=True),
            [{'name': 'config-changed', 'data': {'value': 'fourth', 'third': '3'}}])

    def test_hooks_disabled_nested_contextmanager(self):
        harness = ops.testing.Harness(RecordingCharm, meta='''
                name: test-charm
            ''', config='''
                options:
                    fifth:
                        type: string
                    sixth:
                        type: string
                ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        # Context manager can be nested, so a test using it can invoke a helper using it.
        with harness.hooks_disabled():
            with harness.hooks_disabled():
                harness.update_config({'fifth': '5'})
            harness.update_config({'sixth': '6'})
        self.assertEqual(harness.charm.get_changes(reset=True), [])

    def test_hooks_disabled_noop(self):
        harness = ops.testing.Harness(RecordingCharm, meta='''
                name: test-charm
            ''', config='''
            options:
                seventh:
                    type: string
                eighth:
                    type: string
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        # If hooks are already disabled, it is a no op, and on exit hooks remain disabled.
        harness.disable_hooks()
        with harness.hooks_disabled():
            harness.update_config({'seventh': '7'})
        harness.update_config({'eighth': '8'})
        self.assertEqual(harness.charm.get_changes(reset=True), [])

    def test_metadata_from_directory(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp))
        metadata_filename = tmp / 'metadata.yaml'
        with metadata_filename.open('wt') as metadata:
            metadata.write(textwrap.dedent('''
            name: my-charm
            requires:
                db:
                    interface: pgsql
            '''))
        harness = self._get_dummy_charm_harness(tmp)
        harness.begin()
        self.assertEqual(list(harness.model.relations), ['db'])
        # The charm_dir also gets set
        self.assertEqual(harness.framework.charm_dir, tmp)

    def test_config_from_directory(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp))
        config_filename = tmp / 'config.yaml'
        with config_filename.open('wt') as config:
            config.write(textwrap.dedent('''
            options:
                opt_str:
                    type: string
                    default: "val"
                opt_str_empty:
                    type: string
                    default: ""
                opt_null:
                    type: string
                    default: null
                opt_bool:
                    type: boolean
                    default: true
                opt_int:
                    type: int
                    default: 1
                opt_float:
                    type: float
                    default: 1.0
                opt_no_default:
                    type: string
            '''))
        harness = self._get_dummy_charm_harness(tmp)
        self.assertEqual(harness.model.config['opt_str'], 'val')
        self.assertEqual(harness.model.config['opt_str_empty'], '')
        self.assertIs(harness.model.config['opt_bool'], True)
        self.assertEqual(harness.model.config['opt_int'], 1)
        self.assertIsInstance(harness.model.config['opt_int'], int)
        self.assertEqual(harness.model.config['opt_float'], 1.0)
        self.assertIsInstance(harness.model.config['opt_float'], float)
        self.assertFalse('opt_null' in harness.model.config)
        self.assertIsNone(harness._backend._config._defaults['opt_null'])
        self.assertIsNone(harness._backend._config._defaults['opt_no_default'])

    def test_set_model_name(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
        ''')
        self.addCleanup(harness.cleanup)
        harness.set_model_name('foo')
        self.assertEqual('foo', harness.model.name)

    def test_set_model_name_after_begin(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
        ''')
        self.addCleanup(harness.cleanup)
        harness.set_model_name('bar')
        harness.begin()
        with self.assertRaises(RuntimeError):
            harness.set_model_name('foo')
        self.assertEqual(harness.model.name, 'bar')

    def test_set_model_uuid_after_begin(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
        ''')
        self.addCleanup(harness.cleanup)
        harness.set_model_name('bar')
        harness.set_model_uuid('96957e90-e006-11eb-ba80-0242ac130004')
        harness.begin()
        with self.assertRaises(RuntimeError):
            harness.set_model_uuid('af0479ea-e006-11eb-ba80-0242ac130004')
        self.assertEqual(harness.model.uuid, '96957e90-e006-11eb-ba80-0242ac130004')

    def test_set_model_info_after_begin(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
        ''')
        self.addCleanup(harness.cleanup)
        harness.set_model_info('foo', '96957e90-e006-11eb-ba80-0242ac130004')
        harness.begin()
        with self.assertRaises(RuntimeError):
            harness.set_model_info('bar', 'af0479ea-e006-11eb-ba80-0242ac130004')
        with self.assertRaises(RuntimeError):
            harness.set_model_info('bar')
        with self.assertRaises(RuntimeError):
            harness.set_model_info(uuid='af0479ea-e006-11eb-ba80-0242ac130004')
        with self.assertRaises(RuntimeError):
            harness.set_model_name('bar')
        with self.assertRaises(RuntimeError):
            harness.set_model_uuid('af0479ea-e006-11eb-ba80-0242ac130004')
        self.assertEqual(harness.model.name, 'foo')
        self.assertEqual(harness.model.uuid, '96957e90-e006-11eb-ba80-0242ac130004')

    def test_add_storage_before_harness_begin(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
                    multiple:
                        range: 1-3
            ''')
        self.addCleanup(harness.cleanup)

        stor_ids = harness.add_storage("test", count=3)
        for s in stor_ids:
            # before begin, adding storage does not attach it.
            self.assertNotIn(s, harness._backend.storage_list("test"))

        with self.assertRaises(ops.ModelError):
            harness._backend.storage_get("test/0", "location")[-6:]

    def test_add_storage_then_harness_begin(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
                    multiple:
                        range: 1-3
            ''')
        self.addCleanup(harness.cleanup)

        harness.add_storage("test", count=3)

        with self.assertRaises(ops.ModelError):
            harness._backend.storage_get("test/0", "location")[-6:]

        harness.begin_with_initial_hooks()
        self.assertEqual(len(harness.charm.observed_events), 3)
        for i in range(3):
            self.assertTrue(isinstance(harness.charm.observed_events[i], ops.StorageAttachedEvent))

        want = str(pathlib.PurePath('test', '0'))
        self.assertEqual(want, harness._backend.storage_get("test/0", "location")[-6:])

    def test_add_storage_not_attached_default(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
            ''')
        self.addCleanup(harness.cleanup)

        harness.add_storage('test')
        harness.begin()
        assert len(harness.model.storages['test']) == 0, \
            'storage should start in detached state and be excluded from storage listing'

    def test_add_storage_without_metadata_key_fails(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)

        with self.assertRaises(RuntimeError) as cm:
            harness.add_storage("test")
        self.assertEqual(
            cm.exception.args[0],
            "the key 'test' is not specified as a storage key in metadata")

    def test_add_storage_after_harness_begin(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
                    multiple:
                        range: 1-3
            ''')
        self.addCleanup(harness.cleanup)

        # Set up initial storage
        harness.add_storage("test")[0]
        harness.begin_with_initial_hooks()
        self.assertEqual(len(harness.charm.observed_events), 1)
        self.assertTrue(isinstance(harness.charm.observed_events[0], ops.StorageAttachedEvent))

        # Add additional storage
        stor_ids = harness.add_storage("test", count=3, attach=True)
        # NOTE: stor_id now reflects the 4th ID.  The 2nd and 3rd IDs are created and
        # used, but not returned by Harness.add_storage.
        # (Should we consider changing its return type?)

        added_indices = {self._extract_storage_index(stor_id) for stor_id in stor_ids}
        self.assertTrue(added_indices.issubset(set(harness._backend.storage_list("test"))))

        for i in ['1', '2', '3']:
            storage_name = f"test/{i}"
            want = str(pathlib.PurePath('test', i))
            self.assertTrue(harness._backend.storage_get(storage_name, "location").endswith(want))
        self.assertEqual(len(harness.charm.observed_events), 4)
        for i in range(1, 4):
            self.assertTrue(isinstance(harness.charm.observed_events[i], ops.StorageAttachedEvent))

    def test_detach_storage(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
            ''')
        self.addCleanup(harness.cleanup)

        # Set up initial storage
        stor_id = harness.add_storage("test")[0]
        harness.begin_with_initial_hooks()
        self.assertEqual(len(harness.charm.observed_events), 1)
        self.assertTrue(isinstance(harness.charm.observed_events[0], ops.StorageAttachedEvent))

        # Detach storage
        harness.detach_storage(stor_id)
        self.assertEqual(len(harness.charm.observed_events), 2)
        self.assertTrue(isinstance(harness.charm.observed_events[1], ops.StorageDetachingEvent))

        # Verify backend functions return appropriate values.
        # Real backend would return info only for actively attached storage units.
        self.assertNotIn(stor_id, harness._backend.storage_list("test"))
        with self.assertRaises(ops.ModelError) as cm:
            harness._backend.storage_get("test/0", "location")
        # Error message modeled after output of
        # "storage-get -s <invalid/inactive id> location" on real deployment
        self.assertEqual(
            cm.exception.args[0],
            'ERROR invalid value "test/0" for option -s: storage not found')

        # Retry detach
        # Since already detached, no more hooks should fire
        harness.detach_storage(stor_id)
        self.assertEqual(len(harness.charm.observed_events), 2)
        self.assertTrue(isinstance(harness.charm.observed_events[1], ops.StorageDetachingEvent))

    def test_detach_storage_before_harness_begin(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
            ''')
        self.addCleanup(harness.cleanup)

        stor_id = harness.add_storage("test")[0]
        with self.assertRaises(RuntimeError) as cm:
            harness.detach_storage(f"test/{stor_id}")
        self.assertEqual(cm.exception.args[0],
                         "cannot detach storage before Harness is initialised")

    def test_storage_with_hyphens_works(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
                test-with-hyphens:
                    type: filesystem
            ''')
        self.addCleanup(harness.cleanup)

        # Set up initial storage
        harness.begin()
        helper = StorageWithHyphensHelper(harness.charm, "helper")
        harness.add_storage("test-with-hyphens", attach=True)[0]

        self.assertEqual(len(helper.changes), 1)

    def test_attach_storage(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
            ''')
        self.addCleanup(harness.cleanup)

        # Set up initial storage
        stor_id = harness.add_storage("test")[0]
        harness.begin_with_initial_hooks()
        self.assertEqual(len(harness.charm.observed_events), 1)
        self.assertTrue(isinstance(harness.charm.observed_events[0], ops.StorageAttachedEvent))

        # Detach storage
        harness.detach_storage(stor_id)
        self.assertEqual(len(harness.charm.observed_events), 2)
        self.assertTrue(isinstance(harness.charm.observed_events[1], ops.StorageDetachingEvent))

        # Re-attach storage
        harness.attach_storage(stor_id)
        self.assertEqual(len(harness.charm.observed_events), 3)
        self.assertTrue(isinstance(harness.charm.observed_events[2], ops.StorageAttachedEvent))

        # Verify backend functions return appropriate values.
        # Real backend would return info only for actively attached storage units.
        self.assertIn(self._extract_storage_index(stor_id), harness._backend.storage_list("test"))
        want = str(pathlib.PurePath('test', '0'))
        self.assertEqual(want, harness._backend.storage_get("test/0", "location")[-6:])

        # Retry attach
        # Since already detached, no more hooks should fire
        harness.attach_storage(stor_id)
        self.assertEqual(len(harness.charm.observed_events), 3)
        self.assertTrue(isinstance(harness.charm.observed_events[2], ops.StorageAttachedEvent))

    def test_attach_storage_before_harness_begin(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
            ''')
        self.addCleanup(harness.cleanup)

        # We deliberately don't guard against attaching storage before the harness begins,
        # as there are legitimate reasons to do so.
        stor_id = harness.add_storage("test")[0]
        self.assertTrue(stor_id)

    def test_remove_storage_before_harness_begin(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
                    multiple:
                        range: 1-3
            ''')
        self.addCleanup(harness.cleanup)

        stor_ids = harness.add_storage("test", count=2)
        harness.remove_storage(stor_ids[0])
        # Note re: delta between real behavior and Harness: Juju doesn't allow removal
        # of the last attached storage unit while a workload is still running.  To more
        # easily allow testing of storage removal, I am presently ignoring this detail.
        # (Otherwise, the user would need to flag somehow that they are intentionally
        # removing the final unit as part of a shutdown procedure, else it'd block the
        # removal.  I'm not sure such behavior is productive.)

        harness.begin_with_initial_hooks()
        # Only one hook will fire; one won't since it was removed
        self.assertEqual(len(harness.charm.observed_events), 1)
        self.assertTrue(isinstance(harness.charm.observed_events[0], ops.StorageAttachedEvent))

    def test_remove_storage_without_metadata_key_fails(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)

        # Doesn't really make sense since we already can't add storage which isn't in the metadata,
        # but included for completeness.
        with self.assertRaises(RuntimeError) as cm:
            harness.remove_storage("test/0")
        self.assertEqual(
            cm.exception.args[0],
            "the key 'test' is not specified as a storage key in metadata")

    def test_remove_storage_after_harness_begin(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
                    multiple:
                        range: 1-3
            ''')
        self.addCleanup(harness.cleanup)

        stor_ids = harness.add_storage("test", count=2)
        harness.begin_with_initial_hooks()
        self.assertEqual(len(harness.charm.observed_events), 2)
        self.assertTrue(isinstance(harness.charm.observed_events[0], ops.StorageAttachedEvent))
        self.assertTrue(isinstance(harness.charm.observed_events[1], ops.StorageAttachedEvent))

        harness.remove_storage(stor_ids[1])
        self.assertEqual(len(harness.charm.observed_events), 3)
        self.assertTrue(isinstance(harness.charm.observed_events[2], ops.StorageDetachingEvent))

        attached_storage_ids = harness._backend.storage_list("test")
        self.assertIn(self._extract_storage_index(stor_ids[0]), attached_storage_ids)
        self.assertNotIn(self._extract_storage_index(stor_ids[1]), attached_storage_ids)

    def _extract_storage_index(self, stor_id):
        return int(stor_id.split('/')[-1])

    def test_remove_detached_storage(self):
        harness = ops.testing.Harness(StorageTester, meta='''
            name: test-app
            requires:
                db:
                    interface: pgsql
            storage:
                test:
                    type: filesystem
                    multiple:
                        range: 1-3
            ''')
        self.addCleanup(harness.cleanup)

        stor_ids = harness.add_storage("test", count=2)
        harness.begin_with_initial_hooks()
        harness.detach_storage(stor_ids[0])
        harness.remove_storage(stor_ids[0])  # Already detached, so won't fire a hook
        self.assertEqual(len(harness.charm.observed_events), 3)
        self.assertTrue(isinstance(harness.charm.observed_events[0], ops.StorageAttachedEvent))
        self.assertTrue(isinstance(harness.charm.observed_events[1], ops.StorageAttachedEvent))
        self.assertTrue(isinstance(harness.charm.observed_events[2], ops.StorageDetachingEvent))

    def test_actions_from_directory(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp))
        actions_filename = tmp / 'actions.yaml'
        with actions_filename.open('wt') as actions:
            actions.write(textwrap.dedent('''
            test:
                description: a dummy action
            '''))
        harness = self._get_dummy_charm_harness(tmp)
        harness.begin()
        self.assertEqual(list(harness.framework.meta.actions), ['test'])
        # The charm_dir also gets set
        self.assertEqual(harness.framework.charm_dir, tmp)

    def _get_dummy_charm_harness(self, tmp):
        self._write_dummy_charm(tmp)
        charm_mod = importlib.import_module('testcharm')
        harness = ops.testing.Harness(charm_mod.MyTestingCharm)
        self.addCleanup(harness.cleanup)
        return harness

    def _write_dummy_charm(self, tmp):
        srcdir = tmp / 'src'
        srcdir.mkdir(0o755)
        charm_filename = srcdir / 'testcharm.py'
        with charm_filename.open('wt') as charmpy:
            # language=Python
            charmpy.write(textwrap.dedent('''
                from ops import CharmBase
                class MyTestingCharm(CharmBase):
                    pass
                '''))
        orig = sys.path[:]
        sys.path.append(str(srcdir))

        def cleanup():
            sys.path = orig
            sys.modules.pop('testcharm')

        self.addCleanup(cleanup)

    def test_actions_passed_in(self):
        harness = ops.testing.Harness(
            ops.CharmBase,
            meta='''
                name: test-app
            ''',
            actions='''
                test-action:
                    description: a dummy test action
            ''')
        self.addCleanup(harness.cleanup)
        self.assertEqual(list(harness.framework.meta.actions), ['test-action'])

    def test_event_context(self):
        class MyCharm(ops.CharmBase):
            def event_handler(self, evt):
                evt.relation.data[evt.relation.app]['foo'] = 'bar'

        harness = ops.testing.Harness(MyCharm, meta='''
            name: test-charm
            requires:
                db:
                    interface: pgsql
            ''')
        harness.begin()
        rel_id = harness.add_relation('db', 'postgresql')
        rel = harness.charm.model.get_relation('db', rel_id)

        event = MagicMock()
        event.relation = rel

        with harness._event_context('my_relation_joined'):
            with self.assertRaises(ops.RelationDataError):
                harness.charm.event_handler(event)

    def test_event_context_inverse(self):
        class MyCharm(ops.CharmBase):
            def __init__(self, framework: ops.Framework):
                super().__init__(framework)
                self.framework.observe(self.on.db_relation_joined,
                                       self._join_db)

            def _join_db(self, event):
                # do things with APIs we cannot easily mock
                raise NotImplementedError

        harness = ops.testing.Harness(MyCharm, meta='''
            name: test-charm
            requires:
                db:
                    interface: pgsql
            ''')
        harness.begin()

        def mock_join_db(event):
            # the harness thinks we're inside a db_relation_joined hook
            # but we want to mock the remote data here:
            with harness._event_context(''):
                # pretend for a moment we're not in a hook context,
                # so the harness will let us:
                print(event.relation.app)
                event.relation.data[harness.charm.app]['foo'] = 'bar'

        harness.charm._join_db = mock_join_db
        rel_id = harness.add_relation('db', 'remote')
        harness.add_relation_unit(rel_id, 'remote/0')
        rel = harness.charm.model.get_relation('db', rel_id)
        self.assertEqual({'foo': 'bar'},
                         harness.get_relation_data(rel_id, 'test-charm'))

        # now we're outside of the hook context:
        assert not harness._backend._hook_is_running
        assert rel.data[harness.charm.app]['foo'] == 'bar'

    def test_relation_set_deletes(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_leader(False)
        rel_id = harness.add_relation('db', 'postgresql')
        harness.update_relation_data(rel_id, 'test-charm/0', {'foo': 'bar'})
        harness.add_relation_unit(rel_id, 'postgresql/0')
        rel = harness.charm.model.get_relation('db', rel_id)
        del rel.data[harness.charm.model.unit]['foo']
        self.assertEqual({}, harness.get_relation_data(rel_id, 'test-charm/0'))

    def test_relation_set_nonstring(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_leader(False)
        rel_id = harness.add_relation('db', 'postgresql')
        for invalid_value in (1, 1.2, {}, [], set(), True, object(), type):
            with self.assertRaises(ops.RelationDataError):
                harness.update_relation_data(rel_id, 'test-charm/0',
                                             {'foo': invalid_value})

    def test_set_workload_version(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: app
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        self.assertIsNone(harness.get_workload_version())
        harness.charm.model.unit.set_workload_version('1.2.3')
        self.assertEqual(harness.get_workload_version(), '1.2.3')

    def test_get_backend_calls(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        # No calls to the backend yet
        self.assertEqual(harness._get_backend_calls(), [])
        rel_id = harness.add_relation('db', 'postgresql')

        self.assertEqual(
            [
                ('relation_ids', 'db'),
                ('relation_list', rel_id),
                ('relation_remote_app_name', 0),
            ],
            harness._get_backend_calls())

        # update_relation_data ensures the cached data for the relation is wiped
        harness.update_relation_data(rel_id, 'test-charm/0', {'foo': 'bar'})
        test_charm_unit = harness.model.get_unit('test-charm/0')
        self.assertEqual(
            [
                ('relation_get', 0, 'test-charm/0', False),
                ('update_relation_data', 0, test_charm_unit, 'foo', 'bar')
            ],
            harness._get_backend_calls(reset=True))
        # add_relation_unit resets the relation_list, but doesn't trigger backend calls
        harness.add_relation_unit(rel_id, 'postgresql/0')
        self.assertEqual([], harness._get_backend_calls(reset=False))
        # however, update_relation_data does, because we are preparing relation-changed
        harness.update_relation_data(rel_id, 'postgresql/0', {'foo': 'bar'})
        pgql_unit = harness.model.get_unit('postgresql/0')

        self.assertEqual(
            harness._get_backend_calls(reset=False), [
                ('relation_ids', 'db'),
                ('relation_list', rel_id),
                ('relation_get', 0, 'postgresql/0', False),
                ('update_relation_data', 0, pgql_unit, 'foo', 'bar')
            ])
        # If we check again, they are still there, but now we reset it
        self.assertEqual(
            harness._get_backend_calls(reset=True), [
                ('relation_ids', 'db'),
                ('relation_list', rel_id),
                ('relation_get', 0, 'postgresql/0', False),
                ('update_relation_data', 0, pgql_unit, 'foo', 'bar')
            ])
        # And the calls are gone
        self.assertEqual(harness._get_backend_calls(), [])

    def test_get_backend_calls_with_kwargs(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            requires:
                db:
                    interface: pgsql
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        unit = harness.charm.model.unit
        # Reset the list, because we don't care what it took to get here
        harness._get_backend_calls(reset=True)
        unit.status = ops.ActiveStatus()
        self.assertEqual(
            [('status_set', 'active', '', {'is_app': False})], harness._get_backend_calls())
        harness.set_leader(True)
        app = harness.charm.model.app
        harness._get_backend_calls(reset=True)
        app.status = ops.ActiveStatus('message')
        self.assertEqual(
            [('is_leader',),
             ('status_set', 'active', 'message', {'is_app': True})],
            harness._get_backend_calls())

    def test_unit_status(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: test-app')
        self.addCleanup(harness.cleanup)
        harness.set_leader(True)
        harness.begin()
        # default status
        self.assertEqual(harness.model.unit.status, ops.MaintenanceStatus(''))
        status = ops.ActiveStatus('message')
        harness.model.unit.status = status
        self.assertEqual(harness.model.unit.status, status)

    def test_app_status(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: test-app')
        self.addCleanup(harness.cleanup)
        harness.set_leader(True)
        harness.begin()
        # default status
        self.assertEqual(harness.model.app.status, ops.UnknownStatus())
        status = ops.ActiveStatus('message')
        harness.model.app.status = status
        self.assertEqual(harness.model.app.status, status)

    def test_populate_oci_resources(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: oci-image
                description: "Image to deploy."
              image2:
                type: oci-image
                description: "Another image."
            ''')
        self.addCleanup(harness.cleanup)
        harness.populate_oci_resources()
        path = harness.model.resources.fetch('image')
        self.assertEqual(path.name, 'contents.yaml')
        self.assertEqual(path.parent.name, 'image')
        with path.open('r') as resource_file:
            contents = yaml.safe_load(resource_file.read())
        self.assertEqual(contents['registrypath'], 'registrypath')
        self.assertEqual(contents['username'], 'username')
        self.assertEqual(contents['password'], 'password')
        path = harness.model.resources.fetch('image2')
        self.assertEqual(path.name, 'contents.yaml')
        self.assertEqual(path.parent.name, 'image2')

    def test_resource_folder_cleanup(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: oci-image
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        harness.populate_oci_resources()
        path = harness.model.resources.fetch('image')
        self.assertTrue(path.exists())
        harness.cleanup()
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())
        self.assertFalse(path.parent.parent.exists())

    def test_container_isdir_and_exists(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            containers:
              foo:
                resource: foo-image
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_can_connect('foo', True)
        c = harness.model.unit.containers['foo']

        dir_path = '/tmp/foo/dir'
        file_path = '/tmp/foo/file'

        self.assertFalse(c.isdir(dir_path))
        self.assertFalse(c.exists(dir_path))
        self.assertFalse(c.isdir(file_path))
        self.assertFalse(c.exists(file_path))

        c.make_dir(dir_path, make_parents=True)
        c.push(file_path, 'data')

        self.assertTrue(c.isdir(dir_path))
        self.assertTrue(c.exists(dir_path))
        self.assertFalse(c.isdir(file_path))
        self.assertTrue(c.exists(file_path))

    def test_add_oci_resource_custom(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: oci-image
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        custom = {
            "registrypath": "custompath",
            "username": "custom_username",
            "password": "custom_password",
        }
        harness.add_oci_resource('image', custom)
        resource = harness.model.resources.fetch('image')
        with resource.open('r') as resource_file:
            contents = yaml.safe_load(resource_file.read())
        self.assertEqual(contents['registrypath'], 'custompath')
        self.assertEqual(contents['username'], 'custom_username')
        self.assertEqual(contents['password'], 'custom_password')

    def test_add_oci_resource_no_image(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: file
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        with self.assertRaises(RuntimeError):
            harness.add_oci_resource("image")
        with self.assertRaises(RuntimeError):
            harness.add_oci_resource("missing-resource")
        self.assertEqual(len(harness._backend._resources_map), 0)

    def test_add_resource_unknown(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: file
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        with self.assertRaises(RuntimeError):
            harness.add_resource('unknown', 'content')

    def test_add_resource_but_oci(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: oci-image
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        with self.assertRaises(RuntimeError):
            harness.add_resource('image', 'content')

    def test_add_resource_string(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: file
                filename: foo.txt
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        harness.add_resource('image', 'foo contents\n')
        path = harness.model.resources.fetch('image')
        self.assertEqual(path.name, 'foo.txt')
        self.assertEqual(path.parent.name, 'image')
        with path.open('rt') as f:
            self.assertEqual('foo contents\n', f.read())

    def test_add_resource_bytes(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: file
                filename: foo.zip
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        raw_contents = b'\xff\xff\x00blah\n'
        harness.add_resource('image', raw_contents)
        path = harness.model.resources.fetch('image')
        self.assertEqual(path.name, 'foo.zip')
        self.assertEqual(path.parent.name, 'image')
        with path.open('rb') as f:
            self.assertEqual(raw_contents, f.read())

    def test_add_resource_unknown_filename(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: file
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        harness.add_resource('image', 'foo contents\n')
        path = harness.model.resources.fetch('image')
        self.assertEqual(path.name, 'image')
        self.assertEqual(path.parent.name, 'image')

    def test_get_pod_spec(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            ''')
        self.addCleanup(harness.cleanup)
        harness.set_leader(True)
        container_spec = {'container': 'spec'}
        k8s_resources = {'k8s': 'spec'}
        harness.model.pod.set_spec(container_spec, k8s_resources)
        self.assertEqual(harness.get_pod_spec(), (container_spec, k8s_resources))

    def test_begin_with_initial_hooks_no_relations(self):
        harness = ops.testing.Harness(RecordingCharm, meta='''
            name: test-app
            ''', config='''
            options:
                foo:
                    description: a config option
                    type: string
            ''')
        self.addCleanup(harness.cleanup)
        harness.update_config({'foo': 'bar'})
        harness.set_leader(True)
        with self.assertRaises(RuntimeError):
            _ = harness.charm
        harness.begin_with_initial_hooks()
        self.assertIsNotNone(harness.charm)
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'install'},
                {'name': 'leader-elected'},
                {'name': 'config-changed', 'data': {'foo': 'bar'}},
                {'name': 'start'},
            ]
        )

    def test_begin_with_initial_hooks_no_relations_not_leader(self):
        harness = ops.testing.Harness(RecordingCharm, meta='''
            name: test-app
            ''', config='''
            options:
                foo:
                    description: a config option
                    type: string
            ''')
        self.addCleanup(harness.cleanup)
        harness.update_config({'foo': 'bar'})
        with self.assertRaises(RuntimeError):
            _ = harness.charm
        harness.begin_with_initial_hooks()
        self.assertIsNotNone(harness.charm)
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'install'},
                {'name': 'leader-settings-changed'},
                {'name': 'config-changed', 'data': {'foo': 'bar'}},
                {'name': 'start'},
            ]
        )

    def test_begin_with_initial_hooks_with_peer_relation(self):
        class PeerCharm(RelationEventCharm):
            def __init__(self, framework):
                super().__init__(framework)
                self.observe_relation_events('peer')
        harness = ops.testing.Harness(PeerCharm, meta='''
            name: test-app
            peers:
              peer:
                interface: app-peer
            ''', config='''
            options:
                foo:
                    description: a config option
                    type: string
            ''')
        self.addCleanup(harness.cleanup)
        harness.update_config({'foo': 'bar'})
        with self.assertRaises(RuntimeError):
            _ = harness.charm
        harness.begin_with_initial_hooks()
        self.assertIsNotNone(harness.charm)
        rel_id = harness.model.get_relation('peer').id
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'install'},
                {'name': 'relation-created',
                 'relation': 'peer',
                 'data': {
                     'relation_id': rel_id,
                     'unit': None,
                     'app': 'test-app',
                 }},
                {'name': 'leader-settings-changed'},
                {'name': 'config-changed', 'data': {'foo': 'bar'}},
                {'name': 'start'},
            ])
        # With a single unit, no peer-relation-joined is fired

    def test_begin_with_initial_hooks_peer_relation_pre_defined(self):
        class PeerCharm(RelationEventCharm):
            def __init__(self, framework):
                super().__init__(framework)
                self.observe_relation_events('peer')
        harness = ops.testing.Harness(PeerCharm, meta='''
            name: test-app
            peers:
              peer:
                interface: app-peer
            ''')
        self.addCleanup(harness.cleanup)
        peer_rel_id = harness.add_relation('peer', 'test-app')
        harness.begin_with_initial_hooks()
        # If the peer relation is already defined by the user, we don't create the relation a
        # second time, but we do still fire relation-created.
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'install'},
                {'name': 'relation-created',
                 'relation': 'peer',
                 'data': {
                     'relation_id': peer_rel_id,
                     'unit': None,
                     'app': 'test-app',
                 }},
                {'name': 'leader-settings-changed'},
                {'name': 'config-changed', 'data': {}},
                {'name': 'start'},
            ])

    def test_begin_with_initial_hooks_relation_charm_with_no_relation(self):
        class CharmWithDB(RelationEventCharm):
            def __init__(self, framework):
                super().__init__(framework)
                self.observe_relation_events('db')
        harness = ops.testing.Harness(CharmWithDB, meta='''
            name: test-app
            requires:
              db:
                interface: sql
            ''')
        self.addCleanup(harness.cleanup)
        harness.set_leader()
        harness.begin_with_initial_hooks()
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'install'},
                {'name': 'leader-elected'},
                {'name': 'config-changed', 'data': {}},
                {'name': 'start'},
            ])

    def test_begin_with_initial_hooks_with_one_relation(self):
        class CharmWithDB(RelationEventCharm):
            def __init__(self, framework):
                super().__init__(framework)
                self.observe_relation_events('db')
        harness = ops.testing.Harness(CharmWithDB, meta='''
            name: test-app
            requires:
              db:
                interface: sql
            ''')
        self.addCleanup(harness.cleanup)
        harness.set_leader()
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'new': 'data'})
        harness.begin_with_initial_hooks()
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'install'},
                {'name': 'relation-created',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': None,
                     'app': 'postgresql',
                 }},
                {'name': 'leader-elected'},
                {'name': 'config-changed', 'data': {}},
                {'name': 'start'},
                {'name': 'relation-joined',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/0',
                     'app': 'postgresql',
                 }},
                {'name': 'relation-changed',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/0',
                     'app': 'postgresql',
                 }},
            ])

    def test_begin_with_initial_hooks_with_application_data(self):
        class CharmWithDB(RelationEventCharm):
            def __init__(self, framework):
                super().__init__(framework)
                self.observe_relation_events('db')
        harness = ops.testing.Harness(CharmWithDB, meta='''
            name: test-app
            requires:
              db:
                interface: sql
            ''')
        self.addCleanup(harness.cleanup)
        harness.set_leader()
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.update_relation_data(rel_id, 'postgresql/0', {'new': 'data'})
        harness.update_relation_data(rel_id, 'postgresql', {'app': 'data'})
        harness.begin_with_initial_hooks()
        self.assertEqual(
            [
                {'name': 'install'},
                {'name': 'relation-created',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': None,
                     'app': 'postgresql',
                 }},
                {'name': 'leader-elected'},
                {'name': 'config-changed', 'data': {}},
                {'name': 'start'},
                {'name': 'relation-changed',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': None,
                     'app': 'postgresql',
                 }},
                {'name': 'relation-joined',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/0',
                     'app': 'postgresql',
                 }},
                {'name': 'relation-changed',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/0',
                     'app': 'postgresql',
                 }},
            ],
            harness.charm.changes)

    def test_begin_with_initial_hooks_with_multiple_units(self):
        class CharmWithDB(RelationEventCharm):
            def __init__(self, framework):
                super().__init__(framework)
                self.observe_relation_events('db')
        harness = ops.testing.Harness(CharmWithDB, meta='''
            name: test-app
            requires:
              db:
                interface: sql
            ''')
        self.addCleanup(harness.cleanup)
        harness.set_leader()
        rel_id = harness.add_relation('db', 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/1')
        harness.update_relation_data(rel_id, 'postgresql/1', {'new': 'data'})
        # We intentionally add 0 after 1 to assert that the code triggers them in order
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.begin_with_initial_hooks()
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'install'},
                {'name': 'relation-created',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': None,
                     'app': 'postgresql',
                 }},
                {'name': 'leader-elected'},
                {'name': 'config-changed', 'data': {}},
                {'name': 'start'},
                {'name': 'relation-joined',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/0',
                     'app': 'postgresql',
                 }},
                {'name': 'relation-changed',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/0',
                     'app': 'postgresql',
                 }},
                {'name': 'relation-joined',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/1',
                     'app': 'postgresql',
                 }},
                {'name': 'relation-changed',
                 'relation': 'db',
                 'data': {
                     'relation_id': rel_id,
                     'unit': 'postgresql/1',
                     'app': 'postgresql',
                 }},
            ])

    def test_begin_with_initial_hooks_multiple_relation_same_endpoint(self):
        class CharmWithDB(RelationEventCharm):
            def __init__(self, framework):
                super().__init__(framework)
                self.observe_relation_events('db')
        harness = ops.testing.Harness(CharmWithDB, meta='''
            name: test-app
            requires:
              db:
                interface: sql
            ''')
        self.addCleanup(harness.cleanup)
        harness.set_leader()
        rel_id_a = harness.add_relation('db', 'pg-a')
        harness.add_relation_unit(rel_id_a, 'pg-a/0')
        rel_id_b = harness.add_relation('db', 'pg-b')
        harness.add_relation_unit(rel_id_b, 'pg-b/0')
        harness.begin_with_initial_hooks()
        changes = harness.charm.changes[:]
        expected_prefix = [
            {'name': 'install'},
        ]
        # The first events are always the same
        self.assertEqual(changes[:len(expected_prefix)], expected_prefix)
        changes = changes[len(expected_prefix):]
        # However, the order of relation-created events can be in any order
        expected_relation_created = [
            {'name': 'relation-created',
             'relation': 'db',
             'data': {
                 'relation_id': rel_id_a,
                 'unit': None,
                 'app': 'pg-a',
             }},
            {'name': 'relation-created',
             'relation': 'db',
             'data': {
                 'relation_id': rel_id_b,
                 'unit': None,
                 'app': 'pg-b',
             }},
        ]
        if changes[:2] != expected_relation_created:
            # change the order
            expected_relation_created = [expected_relation_created[1],
                                         expected_relation_created[0]]
        self.assertEqual(changes[:2], expected_relation_created)
        changes = changes[2:]
        expected_middle = [
            {'name': 'leader-elected'},
            {'name': 'config-changed', 'data': {}},
            {'name': 'start'},
        ]
        self.assertEqual(changes[:len(expected_middle)], expected_middle)
        changes = changes[len(expected_middle):]
        a_first = [
            {'name': 'relation-joined',
             'relation': 'db',
             'data': {
                 'relation_id': rel_id_a,
                 'unit': 'pg-a/0',
                 'app': 'pg-a',
             }},
            {'name': 'relation-changed',
             'relation': 'db',
             'data': {
                 'relation_id': rel_id_a,
                 'unit': 'pg-a/0',
                 'app': 'pg-a',
             }},
            {'name': 'relation-joined',
             'relation': 'db',
             'data': {
                 'relation_id': rel_id_b,
                 'unit': 'pg-b/0',
                 'app': 'pg-b',
             }},
            {'name': 'relation-changed',
             'relation': 'db',
             'data': {
                 'relation_id': rel_id_b,
                 'unit': 'pg-b/0',
                 'app': 'pg-b',
             }},
        ]
        if changes != a_first:
            b_first = [a_first[2], a_first[3], a_first[0], a_first[1]]
            self.assertEqual(changes, b_first)

    def test_begin_with_initial_hooks_unknown_status(self):
        # Verify that a charm that does not set a status in the install hook will have an
        # unknown status in the harness.
        harness = ops.testing.Harness(RecordingCharm, meta='''
            name: test-app
            ''', config='''
          options:
                foo:
                    description: a config option
                    type: string
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        harness.begin_with_initial_hooks()

        self.assertEqual(
            backend.status_get(is_app=False),
            {'status': 'unknown', 'message': ''})

        self.assertEqual(
            backend.status_get(is_app=True),
            {'status': 'unknown', 'message': ''})

    def test_begin_with_initial_hooks_install_sets_status(self):
        harness = ops.testing.Harness(RecordingCharm, meta='''
            name: test-app
            ''', config='''
            options:
                set_status:
                    description: a config option
                    type: boolean

            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        harness.update_config(key_values={"set_status": True})
        harness.begin_with_initial_hooks()

        self.assertEqual(
            backend.status_get(is_app=False),
            {'status': 'maintenance', 'message': 'Status set on install'})

    def test_get_pebble_container_plan(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            containers:
              foo:
                resource: foo-image
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_can_connect('foo', True)
        initial_plan = harness.get_container_pebble_plan('foo')
        self.assertEqual(initial_plan.to_yaml(), '{}\n')
        container = harness.model.unit.get_container('foo')
        container.pebble.add_layer('test-ab', '''\
            summary: test-layer
            description: a layer that we can use for testing
            services:
              a:
                command: /bin/echo hello from a
              b:
                command: /bin/echo hello from b
            ''')
        container.pebble.add_layer('test-c', '''\
            summary: test-for-c
            services:
              c:
                command: /bin/echo hello from c
            ''')
        plan = container.pebble.get_plan()
        self.assertEqual(plan.to_yaml(), textwrap.dedent('''\
            services:
              a:
                command: /bin/echo hello from a
              b:
                command: /bin/echo hello from b
              c:
                command: /bin/echo hello from c
            '''))
        harness_plan = harness.get_container_pebble_plan('foo')
        self.assertEqual(harness_plan.to_yaml(), plan.to_yaml())

    def test_get_pebble_container_plan_unknown(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            containers:
              foo:
                resource: foo-image
            ''')
        self.addCleanup(harness.cleanup)
        harness.begin()
        harness.set_can_connect('foo', True)
        with self.assertRaises(KeyError):
            harness.get_container_pebble_plan('unknown')
        plan = harness.get_container_pebble_plan('foo')
        self.assertEqual(plan.to_yaml(), "{}\n")

    def test_container_pebble_ready(self):
        harness = ops.testing.Harness(ContainerEventCharm, meta='''
            name: test-app
            containers:
              foo:
                resource: foo-image
        ''')
        self.addCleanup(harness.cleanup)
        # This is a no-op if it is called before begin(), but it isn't an error
        harness.container_pebble_ready('foo')
        harness.begin()
        harness.charm.observe_container_events('foo')
        harness.container_pebble_ready('foo')
        self.assertEqual(
            harness.charm.changes,
            [
                {'name': 'pebble-ready',
                 'container': 'foo',
                 },
            ]
        )

    def test_get_filesystem_root(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            containers:
              foo:
                resource: foo-image
        ''')
        foo_root = harness.get_filesystem_root("foo")
        self.assertTrue(foo_root.exists())
        self.assertTrue(foo_root.is_dir())
        harness.begin()
        container = harness.charm.unit.get_container("foo")
        self.assertEqual(foo_root, harness.get_filesystem_root(container))


class TestNetwork(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            requires:
               db:
                 interface: database
               foo:
                 interface: xyz
            ''')
        self.addCleanup(self.harness.cleanup)

    def test_add_network_defaults(self):
        self.harness.add_network('10.0.0.10')

        binding = self.harness.model.get_binding('db')
        self.assertEqual(binding.name, 'db')
        network = binding.network
        self.assertEqual(network.bind_address, ipaddress.IPv4Address('10.0.0.10'))
        self.assertEqual(network.ingress_address, ipaddress.IPv4Address('10.0.0.10'))
        self.assertEqual(network.ingress_addresses, [ipaddress.IPv4Address('10.0.0.10')])
        self.assertEqual(network.egress_subnets, [ipaddress.IPv4Network('10.0.0.0/24')])
        self.assertEqual(len(network.interfaces), 1)
        interface = network.interfaces[0]
        self.assertEqual(interface.name, 'eth0')
        self.assertEqual(interface.address, ipaddress.IPv4Address('10.0.0.10'))
        self.assertEqual(interface.subnet, ipaddress.IPv4Network('10.0.0.0/24'))

    def test_add_network_all_args(self):
        relation_id = self.harness.add_relation('db', 'postgresql')
        self.harness.add_network('10.0.0.10',
                                 endpoint='db',
                                 relation_id=relation_id,
                                 cidr='10.0.0.0/8',
                                 interface='eth1',
                                 ingress_addresses=['10.0.0.1', '10.0.0.2'],
                                 egress_subnets=['10.0.0.0/8', '10.10.0.0/16'])

        relation = self.harness.model.get_relation('db', relation_id)
        binding = self.harness.model.get_binding(relation)
        self.assertEqual(binding.name, 'db')
        network = binding.network
        self.assertEqual(network.bind_address, ipaddress.IPv4Address('10.0.0.10'))
        self.assertEqual(network.ingress_address, ipaddress.IPv4Address('10.0.0.1'))
        self.assertEqual(network.ingress_addresses,
                         [ipaddress.IPv4Address('10.0.0.1'), ipaddress.IPv4Address('10.0.0.2')])
        self.assertEqual(network.egress_subnets,
                         [ipaddress.IPv4Network('10.0.0.0/8'),
                          ipaddress.IPv4Network('10.10.0.0/16')])
        self.assertEqual(len(network.interfaces), 1)
        interface = network.interfaces[0]
        self.assertEqual(interface.name, 'eth1')
        self.assertEqual(interface.address, ipaddress.IPv4Address('10.0.0.10'))
        self.assertEqual(interface.subnet, ipaddress.IPv4Network('10.0.0.0/8'))

    def test_add_network_specific_endpoint(self):
        self.harness.add_network('10.0.0.1')
        self.harness.add_network('10.0.2.1', endpoint='db')

        binding = self.harness.model.get_binding('db')
        self.assertEqual(binding.name, 'db')
        network = binding.network
        self.assertEqual(network.bind_address, ipaddress.IPv4Address('10.0.2.1'))

        # Ensure binding for the other interface is still on the default value
        self.assertEqual(self.harness.model.get_binding('foo').network.bind_address,
                         ipaddress.IPv4Address('10.0.0.1'))

    def test_add_network_specific_relation(self):
        self.harness.add_network('10.0.0.1')
        self.harness.add_network('10.0.2.1', endpoint='db')
        relation_id = self.harness.add_relation('db', 'postgresql')
        self.harness.add_network('35.0.0.1', endpoint='db', relation_id=relation_id)

        relation = self.harness.model.get_relation('db', relation_id)
        binding = self.harness.model.get_binding(relation)
        self.assertEqual(binding.name, 'db')
        network = binding.network
        self.assertEqual(network.bind_address, ipaddress.IPv4Address('35.0.0.1'))

        # Ensure binding for the other interface is still on the default value
        self.assertEqual(self.harness.model.get_binding('foo').network.bind_address,
                         ipaddress.IPv4Address('10.0.0.1'))

    def test_add_network_endpoint_fallback(self):
        relation_id = self.harness.add_relation('db', 'postgresql')
        self.harness.add_network('10.0.0.10', endpoint='db')

        relation = self.harness.model.get_relation('db', relation_id)
        binding = self.harness.model.get_binding(relation)
        self.assertEqual(binding.name, 'db')
        network = binding.network
        self.assertEqual(network.bind_address, ipaddress.IPv4Address('10.0.0.10'))

    def test_add_network_default_fallback(self):
        self.harness.add_network('10.0.0.10')

        binding = self.harness.model.get_binding('db')
        self.assertEqual(binding.name, 'db')
        network = binding.network
        self.assertEqual(network.bind_address, ipaddress.IPv4Address('10.0.0.10'))

    def test_add_network_ipv6(self):
        self.harness.add_network('2001:0db8::a:0:0:1')

        binding = self.harness.model.get_binding('db')
        self.assertEqual(binding.name, 'db')
        network = binding.network
        self.assertEqual(network.bind_address, ipaddress.IPv6Address('2001:0db8::a:0:0:1'))
        self.assertEqual(network.ingress_address, ipaddress.IPv6Address('2001:0db8::a:0:0:1'))
        self.assertEqual(network.ingress_addresses, [ipaddress.IPv6Address('2001:0db8::a:0:0:1')])
        self.assertEqual(network.egress_subnets, [ipaddress.IPv6Network('2001:0db8::0:0:0:0/64')])
        self.assertEqual(len(network.interfaces), 1)
        interface = network.interfaces[0]
        self.assertEqual(interface.name, 'eth0')
        self.assertEqual(interface.address, ipaddress.IPv6Address('2001:0db8::a:0:0:1'))
        self.assertEqual(interface.subnet, ipaddress.IPv6Network('2001:0db8::0:0:0:0/64'))

    def test_network_get_relation_not_found(self):
        with self.assertRaises(ops.RelationNotFoundError):
            self.harness.model.get_binding('db').network

    def test_add_network_endpoint_not_in_meta(self):
        with self.assertRaises(ops.ModelError):
            self.harness.add_network('35.0.0.1', endpoint='xyz')

    def test_add_network_relation_id_set_endpoint_not_set(self):
        relation_id = self.harness.add_relation('db', 'postgresql')
        with self.assertRaises(TypeError):
            self.harness.add_network('35.0.0.1', relation_id=relation_id)

    def test_add_network_relation_id_incorrect(self):
        relation_id = self.harness.add_relation('db', 'postgresql')
        with self.assertRaises(ops.ModelError):
            self.harness.add_network('35.0.0.1', endpoint='db', relation_id=relation_id + 1)

    def test_add_network_endpoint_and_relation_id_do_not_correspond(self):
        relation_id = self.harness.add_relation('db', 'postgresql')
        with self.assertRaises(ops.ModelError):
            self.harness.add_network('35.0.0.1', endpoint='foo', relation_id=relation_id)


class DBRelationChangedHelper(ops.Object):
    def __init__(self, parent, key):
        super().__init__(parent, key)
        self.changes = []
        parent.framework.observe(parent.on.db_relation_changed, self.on_relation_changed)

    def on_relation_changed(self, event):
        if event.unit is not None:
            self.changes.append((event.relation.id, event.unit.name))
        else:
            self.changes.append((event.relation.id, event.app.name))


class RelationChangedViewer(ops.Object):
    """Track relation_changed events and saves the data seen in the relation bucket."""

    def __init__(self, charm, relation_name):
        super().__init__(charm, relation_name)
        self.changes = []
        charm.framework.observe(charm.on[relation_name].relation_changed, self.on_relation_changed)

    def on_relation_changed(self, event):
        if event.unit is not None:
            data = event.relation.data[event.unit]
        else:
            data = event.relation.data[event.app]
        self.changes.append(dict(data))


class RecordingCharm(ops.CharmBase):
    """Record the events that we see, and any associated data."""

    def __init__(self, framework):
        super().__init__(framework)
        self.changes = []
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.leader_elected, self._on_leader_elected)
        self.framework.observe(self.on.leader_settings_changed, self._on_leader_settings_changed)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.stop, self._on_stop)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.update_status, self._on_update_status)

    def get_changes(self, reset=True):
        changes = self.changes
        if reset:
            self.changes = []
        return changes

    def _on_install(self, _):
        if self.config.get('set_status'):
            self.unit.status = ops.MaintenanceStatus("Status set on install")
        self.changes.append(dict(name='install'))

    def _on_start(self, _):
        self.changes.append(dict(name='start'))

    def _on_stop(self, _):
        self.changes.append(dict(name='stop'))

    def _on_remove(self, _):
        self.changes.append(dict(name='remove'))

    def _on_config_changed(self, _):
        self.changes.append(dict(name='config-changed', data=dict(self.framework.model.config)))

    def _on_leader_elected(self, _):
        self.changes.append(dict(name='leader-elected'))

    def _on_leader_settings_changed(self, _):
        self.changes.append(dict(name='leader-settings-changed'))

    def _on_upgrade_charm(self, _):
        self.changes.append(dict(name='upgrade-charm'))

    def _on_update_status(self, _):
        self.changes.append(dict(name='update-status'))


class RelationEventCharm(RecordingCharm):
    """Record events related to relation lifecycles."""

    def __init__(self, framework):
        super().__init__(framework)
        # When set, this instructs the charm to include a 'relation_data' field in the 'data'
        # section of each change it logs, which allows us to test which relation data was available
        # in each hook invocation
        self.record_relation_data_on_events = False

    def observe_relation_events(self, relation_name):
        self.relation_name = relation_name
        self.framework.observe(self.on[relation_name].relation_created, self._on_relation_created)
        self.framework.observe(self.on[relation_name].relation_joined, self._on_relation_joined)
        self.framework.observe(self.on[relation_name].relation_changed, self._on_relation_changed)
        self.framework.observe(self.on[relation_name].relation_departed,
                               self._on_relation_departed)
        self.framework.observe(self.on[relation_name].relation_broken, self._on_relation_broken)

    def _on_relation_created(self, event):
        self._observe_relation_event('relation-created', event)

    def _on_relation_joined(self, event):
        self._observe_relation_event('relation-joined', event)

    def _on_relation_changed(self, event):
        self._observe_relation_event('relation-changed', event)

    def _on_relation_departed(self, event):
        self._observe_relation_event('relation-departed', event)

    def _on_relation_broken(self, event):
        self._observe_relation_event('relation-broken', event)

    def _observe_relation_event(self, event_name, event):
        unit_name = None
        if event.unit is not None:
            unit_name = event.unit.name
        app_name = None
        if event.app is not None:
            app_name = event.app.name

        data = dict(app=app_name, unit=unit_name, relation_id=event.relation.id)
        if isinstance(event, ops.RelationDepartedEvent):
            data['departing_unit'] = event.departing_unit.name

        recording = dict(name=event_name, relation=event.relation.name, data=data)

        if self.record_relation_data_on_events:
            recording["data"].update({'relation_data': {
                str(x.name): dict(event.relation.data[x])
                for x in event.relation.data
            }})

        self.changes.append(recording)


class RelationBrokenTester(RelationEventCharm):
    """Access inaccessible relation data."""

    def __init__(self, framework):
        super().__init__(framework)

    def _on_relation_broken(self, event):
        print(event.relation.data[event.relation.app]['bar'])


class ContainerEventCharm(RecordingCharm):
    """Record events related to container lifecycles."""

    def __init__(self, framework):
        super().__init__(framework)

    def observe_container_events(self, container_name):
        self.framework.observe(self.on[container_name].pebble_ready, self._on_pebble_ready)

    def _on_pebble_ready(self, event):
        self._observe_container_event('pebble-ready', event)

    def _observe_container_event(self, event_name, event: ops.PebbleReadyEvent):
        container_name = None
        if event.workload is not None:
            container_name = event.workload.name
        self.changes.append(
            dict(name=event_name, container=container_name))


def get_public_methods(obj):
    """Get the public attributes of obj to compare to another object."""
    public = set()
    members = inspect.getmembers(obj)
    for name, member in members:
        if name.startswith('_'):
            continue
        if inspect.isfunction(member) or inspect.ismethod(member):
            public.add(name)
    return public


class TestTestingModelBackend(unittest.TestCase):

    def test_conforms_to_model_backend(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: app
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        mb_methods = get_public_methods(_ModelBackend)
        backend_methods = get_public_methods(backend)
        self.assertEqual(mb_methods, backend_methods)

    def test_model_uuid_is_uuid_v4(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
        ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        self.assertEqual(uuid.UUID(backend.model_uuid).version, 4)

    def test_status_set_get_unit(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: app
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        backend.status_set('blocked', 'message', is_app=False)
        self.assertEqual(
            backend.status_get(is_app=False),
            {'status': 'blocked', 'message': 'message'})
        self.assertEqual(
            backend.status_get(is_app=True),
            {'status': 'unknown', 'message': ''})

    def test_status_set_get_app(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: app
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        backend.status_set('blocked', 'message', is_app=True)
        self.assertEqual(
            backend.status_get(is_app=True),
            {'status': 'blocked', 'message': 'message'})
        self.assertEqual(
            backend.status_get(is_app=False),
            {'status': 'maintenance', 'message': ''})

    def test_relation_ids_unknown_relation(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            provides:
              db:
                interface: mydb
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        # With no relations added, we just get an empty list for the interface
        self.assertEqual(backend.relation_ids('db'), [])
        # But an unknown interface raises a ModelError
        with self.assertRaises(ops.ModelError):
            backend.relation_ids('unknown')

    def test_relation_get_unknown_relation_id(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        with self.assertRaises(ops.RelationNotFoundError):
            backend.relation_get(1234, 'unit/0', False)

    def test_relation_list_unknown_relation_id(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        with self.assertRaises(ops.RelationNotFoundError):
            backend.relation_list(1234)

    def test_lazy_resource_directory(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: oci-image
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        harness.populate_oci_resources()
        backend = harness._backend
        self.assertIsNone(backend._resource_dir)
        path = backend.resource_get('image')
        self.assertIsNotNone(backend._resource_dir)
        self.assertTrue(
            str(path).startswith(str(backend._resource_dir.name)),
            msg=f'expected {path} to be a subdirectory of {backend._resource_dir.name}')

    def test_resource_get_no_resource(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            resources:
              image:
                type: file
                description: "Image to deploy."
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend
        with self.assertRaises(ops.ModelError) as cm:
            backend.resource_get('foo')
        self.assertIn(
            "units/unit-test-app-0/resources/foo: resource#test-app/foo not found",
            str(cm.exception))

    def test_relation_remote_app_name(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-charm
            requires:
               db:
                 interface: foo
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend

        self.assertIs(backend.relation_remote_app_name(1), None)

        rel_id = harness.add_relation('db', 'postgresql')
        self.assertEqual(backend.relation_remote_app_name(rel_id), 'postgresql')
        harness.add_relation_unit(rel_id, 'postgresql/0')
        harness.add_relation_unit(rel_id, 'postgresql/1')
        self.assertEqual(backend.relation_remote_app_name(rel_id), 'postgresql')

        self.assertIs(backend.relation_remote_app_name(7), None)

    def test_get_pebble_methods(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend

        client = backend.get_pebble('/custom/socket/path')
        self.assertIsInstance(client, _TestingPebbleClient)


class _TestingPebbleClientMixin:
    def get_testing_client(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            containers:
              mycontainer: {}
            ''')
        self.addCleanup(harness.cleanup)
        backend = harness._backend

        client = backend.get_pebble('/charm/containers/mycontainer/pebble.socket')
        harness.set_can_connect('mycontainer', True)
        return client


# For testing non file ops of the pebble testing client.
class TestTestingPebbleClient(unittest.TestCase, _TestingPebbleClientMixin):

    def test_methods_match_pebble_client(self):
        client = self.get_testing_client()
        self.assertIsNotNone(client)
        pebble_client_methods = get_public_methods(pebble.Client)
        testing_client_methods = get_public_methods(client)
        self.assertEqual(pebble_client_methods, testing_client_methods)

    def test_add_layer(self):
        client = self.get_testing_client()
        plan = client.get_plan()
        self.assertIsInstance(plan, pebble.Plan)
        self.assertEqual('{}\n', plan.to_yaml())
        client.add_layer('foo', pebble.Layer('''\
            summary: Foo
            description: |
              A longer description about Foo
            services:
              serv:
                summary: Serv
                description: |
                  A description about Serv the amazing service.
                startup: enabled
                override: replace
                command: '/bin/echo hello'
                environment:
                  KEY: VALUE
            '''))
        plan = client.get_plan()
        # The YAML should be normalized
        self.assertEqual(textwrap.dedent('''\
            services:
              serv:
                command: /bin/echo hello
                description: 'A description about Serv the amazing service.

                  '
                environment:
                  KEY: VALUE
                override: replace
                startup: enabled
                summary: Serv
            '''), plan.to_yaml())

    def test_add_layer_merge(self):
        client = self.get_testing_client()
        plan = client.get_plan()
        self.assertIsInstance(plan, pebble.Plan)
        self.assertEqual('{}\n', plan.to_yaml())
        client.add_layer('foo', pebble.Layer('''\
            summary: Foo
            description: |
              A longer description about Foo
            services:
              serv:
                summary: Serv
                description: |
                  A description about Serv the amazing service.
                startup: enabled
                override: replace
                command: '/bin/echo hello'
                environment:
                  KEY1: VALUE1
                after:
                - thing1
                before:
                - thing1
                requires:
                - thing1
                user: user1
                user-id: userID1
                group: group1
                group-id: groupID1
                on-failure: thing1
                on-success: thing1
                on-check-failure:
                  KEY1: VALUE1
                backoff-delay: 1
                backoff-factor: 2
                backoff-limit: 1
            '''))
        plan = client.get_plan()
        # The YAML should be normalized
        self.maxDiff = None
        self.assertEqual(textwrap.dedent('''\
            services:
              serv:
                after:
                - thing1
                backoff-delay: 1
                backoff-factor: 2
                backoff-limit: 1
                before:
                - thing1
                command: /bin/echo hello
                description: 'A description about Serv the amazing service.

                  '
                environment:
                  KEY1: VALUE1
                group: group1
                group-id: groupID1
                on-check-failure:
                  KEY1: VALUE1
                on-failure: thing1
                on-success: thing1
                override: replace
                requires:
                - thing1
                startup: enabled
                summary: Serv
                user: user1
                user-id: userID1
            '''), plan.to_yaml())

        client.add_layer('foo', pebble.Layer('''\
            summary: Foo
            description: |
              A longer description about Foo
            services:
              serv:
                summary: Serv
                description: |
                  A new description of the the amazing Serv service.
                startup: enabled
                override: merge
                command: '/bin/echo hello'
                environment:
                  KEY1: VALUE4
                  KEY2: VALUE2
                  KEY3: VALUE3
                after:
                - thing2
                before:
                - thing2
                requires:
                - thing2
                user: user2
                user-id: userID2
                group: group2
                group-id: groupID2
                on-success: thing2
                on-failure: thing2
                on-check-failure:
                  KEY1: VALUE4
                  KEY2: VALUE2
                  KEY3: VALUE3
                backoff-delay: 2
                backoff-factor: 3
                backoff-limit: 2
            '''), combine=True)
        plan = client.get_plan()
        # The YAML should be normalized
        self.assertEqual(textwrap.dedent('''\
            services:
              serv:
                after:
                - thing1
                - thing2
                backoff-delay: 2
                backoff-factor: 3
                backoff-limit: 2
                before:
                - thing1
                - thing2
                command: /bin/echo hello
                description: 'A new description of the the amazing Serv service.

                  '
                environment:
                  KEY1: VALUE4
                  KEY2: VALUE2
                  KEY3: VALUE3
                group: group2
                group-id: groupID2
                on-check-failure:
                  KEY1: VALUE4
                  KEY2: VALUE2
                  KEY3: VALUE3
                on-failure: thing2
                on-success: thing2
                override: merge
                requires:
                - thing1
                - thing2
                startup: enabled
                summary: Serv
                user: user2
                user-id: userID2
            '''), plan.to_yaml())

    def test_add_layer_not_combined(self):
        client = self.get_testing_client()
        plan = client.get_plan()
        self.assertIsInstance(plan, pebble.Plan)
        self.assertEqual('{}\n', plan.to_yaml())
        service = textwrap.dedent('''\
            summary: Foo
            description: |
              A longer description about Foo
            services:
              serv:
                summary: Serv
                description: |
                  A description about Serv the amazing service.
                startup: enabled
                override: replace
                command: '/bin/echo hello'
                environment:
                  KEY: VALUE
            ''')
        client.add_layer('foo', pebble.Layer(service))
        # TODO: jam 2021-04-19 We should have a clearer error type for this case. The actual
        #  pebble raises an HTTP exception. See https://github.com/canonical/operator/issues/514
        #  that this should be cleaned up into a clearer error type, however, they should get an
        #  error
        with self.assertRaises(RuntimeError):
            client.add_layer('foo', pebble.Layer(service))

    def test_add_layer_three_services(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                override: replace
                command: '/bin/echo foo'
            ''')
        client.add_layer('bar', '''\
            summary: bar
            services:
              bar:
                summary: The Great Bar
                startup: enabled
                override: replace
                command: '/bin/echo bar'
            ''')
        client.add_layer('baz', '''\
            summary: baz
            services:
              baz:
                summary: Not Bar, but Baz
                startup: enabled
                override: replace
                command: '/bin/echo baz'
            ''')
        plan = client.get_plan()
        self.maxDiff = 1000
        # Alphabetical services, and the YAML should be normalized
        self.assertEqual(textwrap.dedent('''\
            services:
              bar:
                command: /bin/echo bar
                override: replace
                startup: enabled
                summary: The Great Bar
              baz:
                command: /bin/echo baz
                override: replace
                startup: enabled
                summary: Not Bar, but Baz
              foo:
                command: /bin/echo foo
                override: replace
                startup: enabled
                summary: Foo
            '''), plan.to_yaml())

    def test_add_layer_combine_no_override(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
            command: '/bin/echo foo'
            ''')
        # TODO: jam 2021-04-19 Pebble currently raises a HTTP Error 500 Internal Service Error
        #  if you don't supply an override directive. That needs to be fixed and this test
        #  should be updated. https://github.com/canonical/operator/issues/514
        with self.assertRaises(RuntimeError):
            client.add_layer('foo', '''\
                summary: foo
                services:
                  foo:
                    summary: Foo
                    command: '/bin/echo foo'
                ''', combine=True)

    def test_add_layer_combine_override_replace(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              bar:
                summary: Bar
                command: '/bin/echo bar'
              foo:
                summary: Foo
                command: '/bin/echo foo'
            ''')
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                command: '/bin/echo foo new'
                override: replace
            ''', combine=True)
        self.assertEqual(textwrap.dedent('''\
            services:
              bar:
                command: /bin/echo bar
                summary: Bar
              foo:
                command: /bin/echo foo new
                override: replace
            '''), client.get_plan().to_yaml())

    def test_add_layer_combine_override_merge(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              bar:
                summary: Bar
                command: '/bin/echo bar'
              foo:
                summary: Foo
                command: '/bin/echo foo'
            ''')
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                command: '/bin/echo foob'
                override: merge
            ''', combine=True)
        self.assertEqual(textwrap.dedent('''\
            services:
              bar:
                command: /bin/echo bar
                summary: Bar
              foo:
                command: /bin/echo foob
                override: merge
                summary: Foo
            '''), client.get_plan().to_yaml())

    def test_add_layer_combine_override_unknown(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              bar:
                summary: Bar
                command: '/bin/echo bar'
              foo:
                summary: Foo
                command: '/bin/echo foo'
            ''')
        with self.assertRaises(RuntimeError):
            client.add_layer('foo', '''\
                summary: foo
                services:
                  foo:
                    summary: Foo
                    command: '/bin/echo foob'
                    override: blah
                ''', combine=True)

    def test_get_services_none(self):
        client = self.get_testing_client()
        service_info = client.get_services()
        self.assertEqual([], service_info)

    def test_get_services_not_started(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        infos = client.get_services()
        self.assertEqual(len(infos), 2)
        bar_info = infos[0]
        self.assertEqual('bar', bar_info.name)
        # Default when not specified is DISABLED
        self.assertEqual(pebble.ServiceStartup.DISABLED, bar_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, bar_info.current)
        self.assertFalse(bar_info.is_running())
        foo_info = infos[1]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, foo_info.current)
        self.assertFalse(foo_info.is_running())

    def test_get_services_autostart(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        client.autostart_services()
        infos = client.get_services()
        self.assertEqual(len(infos), 2)
        bar_info = infos[0]
        self.assertEqual('bar', bar_info.name)
        # Default when not specified is DISABLED
        self.assertEqual(pebble.ServiceStartup.DISABLED, bar_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, bar_info.current)
        self.assertFalse(bar_info.is_running())
        foo_info = infos[1]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.ACTIVE, foo_info.current)
        self.assertTrue(foo_info.is_running())

    def test_get_services_start_stop(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        client.start_services(['bar'])
        infos = client.get_services()
        self.assertEqual(len(infos), 2)
        bar_info = infos[0]
        self.assertEqual('bar', bar_info.name)
        # Even though bar defaults to DISABLED, we explicitly started it
        self.assertEqual(pebble.ServiceStartup.DISABLED, bar_info.startup)
        self.assertEqual(pebble.ServiceStatus.ACTIVE, bar_info.current)
        # foo would be started by autostart, but we only called start_services
        foo_info = infos[1]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, foo_info.current)
        client.stop_services(['bar'])
        infos = client.get_services()
        bar_info = infos[0]
        self.assertEqual('bar', bar_info.name)
        self.assertEqual(pebble.ServiceStartup.DISABLED, bar_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, bar_info.current)

    def test_get_services_bad_request(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        # It is a common mistake to pass just a name vs a list of names, so catch it with a
        # TypeError
        with self.assertRaises(TypeError):
            client.get_services('foo')

    def test_get_services_subset(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        infos = client.get_services(['foo'])
        self.assertEqual(len(infos), 1)
        foo_info = infos[0]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, foo_info.current)

    def test_get_services_unknown(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        # This doesn't seem to be an error at the moment.
        # pebble_cli.py service just returns an empty list
        # pebble service unknown says "No matching services" (but exits 0)
        infos = client.get_services(['unknown'])
        self.assertEqual(infos, [])

    def test_invalid_start_service(self):
        client = self.get_testing_client()
        # TODO: jam 2021-04-20 This should become a better error
        with self.assertRaises(RuntimeError):
            client.start_services(['unknown'])

    def test_start_service_str(self):
        # Start service takes a list of names, but it is really easy to accidentally pass just a
        # name
        client = self.get_testing_client()
        with self.assertRaises(TypeError):
            client.start_services('unknown')

    def test_stop_service_str(self):
        # Start service takes a list of names, but it is really easy to accidentally pass just a
        # name
        client = self.get_testing_client()
        with self.assertRaises(TypeError):
            client.stop_services('unknown')

    def test_mixed_start_service(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
            ''')
        # TODO: jam 2021-04-20 better error type
        with self.assertRaises(RuntimeError):
            client.start_services(['foo', 'unknown'])
        # foo should not be started
        infos = client.get_services()
        self.assertEqual(len(infos), 1)
        foo_info = infos[0]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, foo_info.current)

    def test_stop_services_unknown(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
            ''')
        client.autostart_services()
        # TODO: jam 2021-04-20 better error type
        with self.assertRaises(RuntimeError):
            client.stop_services(['foo', 'unknown'])
        # foo should still be running
        infos = client.get_services()
        self.assertEqual(len(infos), 1)
        foo_info = infos[0]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.ACTIVE, foo_info.current)

    def test_start_started_service(self):
        # Pebble maintains idempotency even if you start a service
        # which is already started.
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        client.autostart_services()
        # Foo is now started, but Bar is not
        client.start_services(['bar', 'foo'])
        # foo and bar are both started
        infos = client.get_services()
        self.assertEqual(len(infos), 2)
        bar_info = infos[0]
        self.assertEqual('bar', bar_info.name)
        # Default when not specified is DISABLED
        self.assertEqual(pebble.ServiceStartup.DISABLED, bar_info.startup)
        self.assertEqual(pebble.ServiceStatus.ACTIVE, bar_info.current)
        foo_info = infos[1]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.ACTIVE, foo_info.current)

    def test_stop_stopped_service(self):
        # Pebble maintains idempotency even if you stop a service
        # which is already stopped.
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        client.autostart_services()
        # Foo is now started, but Bar is not
        client.stop_services(['foo', 'bar'])
        # foo and bar are both stopped
        infos = client.get_services()
        self.assertEqual(len(infos), 2)
        bar_info = infos[0]
        self.assertEqual('bar', bar_info.name)
        # Default when not specified is DISABLED
        self.assertEqual(pebble.ServiceStartup.DISABLED, bar_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, bar_info.current)
        foo_info = infos[1]
        self.assertEqual('foo', foo_info.name)
        self.assertEqual(pebble.ServiceStartup.ENABLED, foo_info.startup)
        self.assertEqual(pebble.ServiceStatus.INACTIVE, foo_info.current)

    @ unittest.skipUnless(is_linux, 'Pebble runs on Linux')
    def test_send_signal(self):
        client = self.get_testing_client()
        client.add_layer('foo', '''\
            summary: foo
            services:
              foo:
                summary: Foo
                startup: enabled
                command: '/bin/echo foo'
              bar:
                summary: Bar
                command: '/bin/echo bar'
            ''')
        client.autostart_services()
        # Foo is now started, but Bar is not

        # Send a valid signal to a running service
        client.send_signal("SIGINT", ("foo",))

        # Send a valid signal but omit service name
        with self.assertRaises(TypeError):
            client.send_signal("SIGINT", tuple())

        # Send an invalid signal to a running service
        with self.assertRaises(pebble.APIError):
            client.send_signal("sigint", ("foo",))

        # Send a valid signal to a stopped service
        with self.assertRaises(pebble.APIError):
            client.send_signal("SIGINT", ("bar",))

        # Send a valid signal to a non-existing service
        with self.assertRaises(pebble.APIError):
            client.send_signal("SIGINT", ("baz",))

        # Send a valid signal to a multiple services, one of which is not running
        with self.assertRaises(pebble.APIError):
            client.send_signal("SIGINT", ("foo", "bar",))


# For testing file-ops of the pebble client.  This is refactored into a
# separate mixin so we can run these tests against both the mock client as
# well as a real pebble server instance.
class _PebbleStorageAPIsTestMixin:
    # Override this in classes using this mixin.
    # This should be set to any non-empty path, but without a trailing /.
    prefix = None

    def test_push_and_pull_bytes(self):
        self._test_push_and_pull_data(
            original_data=b"\x00\x01\x02\x03\x04",
            encoding=None,
            stream_class=io.BytesIO)

    def test_push_and_pull_non_utf8_data(self):
        self._test_push_and_pull_data(
            original_data='日本語',  # "Japanese" in Japanese
            encoding='sjis',
            stream_class=io.StringIO)

    def _test_push_and_pull_data(self, original_data, encoding, stream_class):
        client = self.client
        client.push(f"{self.prefix}/test", original_data, encoding=encoding)
        with client.pull(f"{self.prefix}/test", encoding=encoding) as infile:
            received_data = infile.read()
        self.assertEqual(original_data, received_data)

        # We also support file-like objects as input, so let's test that case as well.
        small_file = stream_class(original_data)
        client.push(f"{self.prefix}/test", small_file, encoding=encoding)
        with client.pull(f"{self.prefix}/test", encoding=encoding) as infile:
            received_data = infile.read()
        self.assertEqual(original_data, received_data)

    def test_push_and_pull_larger_file(self):
        # Intent: to ensure things work appropriately with larger files.
        # Larger files may be sent/received in multiple chunks; this should help for
        # checking that such logic is correct.
        data_size = 1024 * 1024
        original_data = os.urandom(data_size)

        client = self.client
        client.push(f"{self.prefix}/test", original_data, encoding=None)
        with client.pull(f"{self.prefix}/test", encoding=None) as infile:
            received_data = infile.read()
        self.assertEqual(original_data, received_data)

    def test_push_to_non_existent_subdir(self):
        data = 'data'
        client = self.client

        with self.assertRaises(pebble.PathError) as cm:
            client.push(f"{self.prefix}/nonexistent_dir/test", data, make_dirs=False)
        self.assertEqual(cm.exception.kind, 'not-found')

        client.push(f"{self.prefix}/nonexistent_dir/test", data, make_dirs=True)

    def test_push_as_child_of_file_raises_error(self):
        data = 'data'
        client = self.client
        client.push(f"{self.prefix}/file", data)
        with self.assertRaises(pebble.PathError) as cm:
            client.push(f"{self.prefix}/file/file", data)
        self.assertEqual(cm.exception.kind, 'generic-file-error')

    def test_push_with_permission_mask(self):
        data = 'data'
        client = self.client
        client.push(f"{self.prefix}/file", data, permissions=0o600)
        client.push(f"{self.prefix}/file", data, permissions=0o777)
        # If permissions are outside of the range 0o000 through 0o777, an exception should be
        # raised.
        for bad_permission in (
            0o1000,  # Exceeds 0o777
            -1,      # Less than 0o000
        ):
            with self.assertRaises(pebble.PathError) as cm:
                client.push(f"{self.prefix}/file", data, permissions=bad_permission)
        self.assertEqual(cm.exception.kind, 'generic-file-error')

    def test_push_files_and_list(self):
        data = 'data'
        client = self.client

        # Let's push the first file with a bunch of details.  We'll check on this later.
        client.push(
            f"{self.prefix}/file1", data,
            permissions=0o620)

        # Do a quick push with defaults for the other files.
        client.push(f"{self.prefix}/file2", data)
        client.push(f"{self.prefix}/file3", data)

        files = client.list_files(f"{self.prefix}/")
        self.assertEqual({file.path for file in files},
                         {self.prefix + file for file in ('/file1', '/file2', '/file3')})

        # Let's pull the first file again and check its details
        file = [f for f in files if f.path == f"{self.prefix}/file1"][0]
        self.assertEqual(file.name, 'file1')
        self.assertEqual(file.type, pebble.FileType.FILE)
        self.assertEqual(file.size, 4)
        self.assertIsInstance(file.last_modified, datetime.datetime)
        self.assertEqual(file.permissions, 0o620)
        # Skipping ownership checks here; ownership will be checked in purely-mocked tests

    def test_push_and_list_file(self):
        data = 'data'
        client = self.client
        client.push(f"{self.prefix}/file", data)
        files = client.list_files(f"{self.prefix}/")
        self.assertEqual({file.path for file in files}, {f"{self.prefix}/file"})

    def test_push_file_with_relative_path_fails(self):
        client = self.client
        with self.assertRaises(pebble.PathError) as cm:
            client.push('file', '')
        self.assertEqual(cm.exception.kind, 'generic-file-error')

    def test_pull_not_found(self):
        with self.assertRaises(pebble.PathError) as cm:
            self.client.pull("/not/found")
        self.assertEqual(cm.exception.kind, "not-found")
        self.assertIn("/not/found", cm.exception.message)

    def test_pull_directory(self):
        self.client.make_dir(f"{self.prefix}/subdir")
        with self.assertRaises(pebble.PathError) as cm:
            self.client.pull(f"{self.prefix}/subdir")
        self.assertEqual(cm.exception.kind, "generic-file-error")
        self.assertIn(f"{self.prefix}/subdir", cm.exception.message)

    def test_list_files_not_found_raises(self):
        client = self.client
        with self.assertRaises(pebble.APIError) as cm:
            client.list_files("/not/existing/file/")
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(cm.exception.status, 'Not Found')
        self.assertEqual(cm.exception.message, 'stat /not/existing/file/: no '
                                               'such file or directory')

    def test_list_directory_object_itself(self):
        client = self.client

        # Test with root dir
        # (Special case; we won't prefix this, even when using the real Pebble server.)
        files = client.list_files('/', itself=True)
        self.assertEqual(len(files), 1)
        dir_ = files[0]
        self.assertEqual(dir_.path, '/')
        self.assertEqual(dir_.name, '/')
        self.assertEqual(dir_.type, pebble.FileType.DIRECTORY)

        # Test with subdirs
        client.make_dir(f"{self.prefix}/subdir")
        files = client.list_files(f"{self.prefix}/subdir", itself=True)
        self.assertEqual(len(files), 1)
        dir_ = files[0]
        self.assertEqual(dir_.name, 'subdir')
        self.assertEqual(dir_.type, pebble.FileType.DIRECTORY)

    def test_push_files_and_list_by_pattern(self):
        # Note: glob pattern deltas do exist between golang and Python, but here,
        # we'll just use a simple * pattern.
        data = 'data'
        client = self.client
        for filename in (
            '/file1.gz',
            '/file2.tar.gz',
            '/file3.tar.bz2',
            '/backup_file.gz',
        ):
            client.push(self.prefix + filename, data)
        files = client.list_files(f"{self.prefix}/", pattern='file*.gz')
        self.assertEqual({file.path for file in files},
                         {self.prefix + file for file in ('/file1.gz', '/file2.tar.gz')})

    def test_make_directory(self):
        client = self.client
        client.make_dir(f"{self.prefix}/subdir")
        self.assertEqual(
            client.list_files(f"{self.prefix}/", pattern='subdir')[0].path,
            f"{self.prefix}/subdir")
        client.make_dir(f"{self.prefix}/subdir/subdir")
        self.assertEqual(
            client.list_files(f"{self.prefix}/subdir", pattern='subdir')[0].path,
            f"{self.prefix}/subdir/subdir")

    def test_make_directory_recursively(self):
        client = self.client

        with self.assertRaises(pebble.PathError) as cm:
            client.make_dir(f"{self.prefix}/subdir/subdir", make_parents=False)
        self.assertEqual(cm.exception.kind, 'not-found')

        client.make_dir(f"{self.prefix}/subdir/subdir", make_parents=True)
        self.assertEqual(
            client.list_files(f"{self.prefix}/subdir", pattern='subdir')[0].path,
            f"{self.prefix}/subdir/subdir")

    def test_make_directory_with_relative_path_fails(self):
        client = self.client
        with self.assertRaises(pebble.PathError) as cm:
            client.make_dir('dir')
        self.assertEqual(cm.exception.kind, 'generic-file-error')

    def test_make_subdir_of_file_fails(self):
        client = self.client
        client.push(f"{self.prefix}/file", 'data')

        # Direct child case
        with self.assertRaises(pebble.PathError) as cm:
            client.make_dir(f"{self.prefix}/file/subdir")
        self.assertEqual(cm.exception.kind, 'generic-file-error')

        # Recursive creation case, in case its flow is different
        with self.assertRaises(pebble.PathError) as cm:
            client.make_dir(f"{self.prefix}/file/subdir/subdir", make_parents=True)
        self.assertEqual(cm.exception.kind, 'generic-file-error')

    def test_make_dir_with_permission_mask(self):
        client = self.client
        client.make_dir(f"{self.prefix}/dir1", permissions=0o700)
        client.make_dir(f"{self.prefix}/dir2", permissions=0o777)

        files = client.list_files(f"{self.prefix}/", pattern='dir*')
        self.assertEqual([f for f in files if f.path == f"{self.prefix}/dir1"]
                         [0].permissions, 0o700)
        self.assertEqual([f for f in files if f.path == f"{self.prefix}/dir2"]
                         [0].permissions, 0o777)

        # If permissions are outside of the range 0o000 through 0o777, an exception should be
        # raised.
        for i, bad_permission in enumerate((
            0o1000,  # Exceeds 0o777
            -1,      # Less than 0o000
        )):
            with self.assertRaises(pebble.PathError) as cm:
                client.make_dir(f"{self.prefix}/dir3_{i}", permissions=bad_permission)
            self.assertEqual(cm.exception.kind, 'generic-file-error')

    def test_remove_path(self):
        client = self.client
        client.push(f"{self.prefix}/file", '')
        client.make_dir(f"{self.prefix}/dir/subdir", make_parents=True)
        client.push(f"{self.prefix}/dir/subdir/file1", '')
        client.push(f"{self.prefix}/dir/subdir/file2", '')
        client.push(f"{self.prefix}/dir/subdir/file3", '')
        client.make_dir(f"{self.prefix}/empty_dir")

        client.remove_path(f"{self.prefix}/file")

        client.remove_path(f"{self.prefix}/empty_dir")

        # Remove non-empty directory, recursive=False: error
        with self.assertRaises(pebble.PathError) as cm:
            client.remove_path(f"{self.prefix}/dir", recursive=False)
        self.assertEqual(cm.exception.kind, 'generic-file-error')

        # Remove non-empty directory, recursive=True: succeeds (and removes child objects)
        client.remove_path(f"{self.prefix}/dir", recursive=True)

        # Remove non-existent path, recursive=False: error
        with self.assertRaises(pebble.PathError) as cm:
            client.remove_path(f"{self.prefix}/dir/does/not/exist/asdf", recursive=False)
        self.assertEqual(cm.exception.kind, 'not-found')

        # Remove non-existent path, recursive=True: succeeds
        client.remove_path(f"{self.prefix}/dir/does/not/exist/asdf", recursive=True)

    # Other notes:
    # * Parent directories created via push(make_dirs=True) default to root:root ownership
    #   and whatever permissions are specified via the permissions argument; as we default to None
    #   for ownership/permissions, we do ignore this nuance.
    # * Parent directories created via make_dir(make_parents=True) default to root:root ownership
    #   and 0o755 permissions; as we default to None for ownership/permissions, we do ignore this
    #   nuance.


class TestPebbleStorageAPIsUsingMocks(
        unittest.TestCase,
        _TestingPebbleClientMixin,
        _PebbleStorageAPIsTestMixin):
    def setUp(self):
        self.prefix = '/prefix'
        self.client = self.get_testing_client()
        if self.prefix:
            self.client.make_dir(self.prefix, make_parents=True)

    def test_container_storage_mounts(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='''
            name: test-app
            containers:
                c1:
                    mounts:
                        - storage: store1
                          location: /mounts/foo
                c2:
                    mounts:
                        - storage: store2
                          location: /mounts/foo
                c3:
                    mounts:
                        - storage: store1
                          location: /mounts/bar
            storage:
                store1:
                    type: filesystem
                store2:
                    type: filesystem
            ''')
        self.addCleanup(harness.cleanup)

        store_id = harness.add_storage('store1')[0]
        harness.attach_storage(store_id)

        harness.begin()
        harness.set_can_connect('c1', True)
        harness.set_can_connect('c2', True)
        harness.set_can_connect('c3', True)

        # push file to c1 storage mount, check that we can see it in charm container storage path.
        c1 = harness.model.unit.get_container('c1')
        c1_fname = 'foo.txt'
        c1_fpath = os.path.join('/mounts/foo', c1_fname)
        c1.push(c1_fpath, '42')
        self.assertTrue(c1.exists(c1_fpath))
        fpath = os.path.join(str(harness.model.storages['store1'][0].location), 'foo.txt')
        with open(fpath) as f:
            self.assertEqual('42', f.read())

        # check that the file is not visible in c2 which has a different storage mount
        c2 = harness.model.unit.get_container('c2')
        c2_fpath = os.path.join('/mounts/foo', c1_fname)
        self.assertFalse(c2.exists(c2_fpath))

        # check that the file is visible in c3 which has the same storage mount
        c3 = harness.model.unit.get_container('c3')
        c3_fpath = os.path.join('/mounts/bar', c1_fname)
        self.assertTrue(c3.exists(c3_fpath))
        with c3.pull(c3_fpath) as f:
            self.assertEqual('42', f.read())

        # test all other container file ops
        with c1.pull(c1_fpath) as f:
            self.assertEqual('42', f.read())
        files = c1.list_files(c1_fpath)
        self.assertEqual([c1_fpath], [fi.path for fi in files])
        c1.remove_path(c1_fpath)
        self.assertFalse(c1.exists(c1_fpath))

        # test detaching storage
        c1.push(c1_fpath, '42')
        self.assertTrue(c1.exists(c1_fpath))
        store1_id = harness.model.storages['store1'][0].full_id
        harness.remove_storage(store1_id)
        self.assertFalse(c1.exists(c1_fpath))

    def _select_testing_user_group(self):
        user = [u for u in pwd.getpwall() if u.pw_uid != os.getuid()][0]
        group = [g for g in grp.getgrall() if g.gr_gid != os.getgid()][0]
        return user, group

    @unittest.skipUnless(os.getuid() == 0, "require root privilege")
    def test_push_with_ownership(self):
        # Note: To simplify implementation, ownership is simply stored as-is with no verification.
        data = 'data'
        client = self.client
        user, group = self._select_testing_user_group()
        client.push(
            f"{self.prefix}/file",
            data,
            user_id=user.pw_uid,
            user=user.pw_name,
            group_id=group.gr_gid,
            group=group.gr_name)
        file_ = client.list_files(f"{self.prefix}/file")[0]
        self.assertEqual(file_.user_id, user.pw_uid)
        self.assertEqual(file_.user, user.pw_name)
        self.assertEqual(file_.group_id, group.gr_gid)
        self.assertEqual(file_.group, group.gr_name)

    @unittest.skipUnless(os.getuid() == 0, "require root privilege")
    def test_make_dir_with_ownership(self):
        client = self.client
        user, group = self._select_testing_user_group()
        client.make_dir(
            f"{self.prefix}/dir1",
            user_id=user.pw_uid,
            user=user.pw_name,
            group_id=group.gr_gid,
            group=group.gr_name)
        dir_ = client.list_files(f"{self.prefix}/dir1", itself=True)[0]
        self.assertEqual(dir_.user_id, user.pw_uid)
        self.assertEqual(dir_.user, user.pw_name)
        self.assertEqual(dir_.group_id, group.gr_gid)
        self.assertEqual(dir_.group, group.gr_name)


@unittest.skipUnless(os.getenv('RUN_REAL_PEBBLE_TESTS'), 'RUN_REAL_PEBBLE_TESTS not set')
class TestPebbleStorageAPIsUsingRealPebble(unittest.TestCase, _PebbleStorageAPIsTestMixin):
    def setUp(self):
        socket_path = os.getenv('PEBBLE_SOCKET')
        pebble_dir = os.getenv('PEBBLE')
        if not socket_path and pebble_dir:
            socket_path = os.path.join(pebble_dir, '.pebble.socket')
        assert socket_path and pebble_dir, 'PEBBLE must be set if RUN_REAL_PEBBLE_TESTS set'

        self.prefix = tempfile.mkdtemp(dir=pebble_dir)
        self.client = pebble.Client(socket_path=socket_path)

    def tearDown(self):
        shutil.rmtree(self.prefix)

    # Remove this entirely once the associated bug is fixed; it overrides the original test in the
    # test mixin class.
    @unittest.skip('pending resolution of https://github.com/canonical/pebble/issues/80')
    def test_make_dir_with_permission_mask(self):
        pass


class TestSecrets(unittest.TestCase):
    def test_add_model_secret_by_app_name_str(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        relation_id = harness.add_relation('db', 'database')
        harness.add_relation_unit(relation_id, 'database/0')

        secret_id = harness.add_model_secret('database', {'password': 'hunter2'})
        harness.grant_secret(secret_id, 'webapp')
        secret = harness.model.get_secret(id=secret_id)
        self.assertEqual(secret.id, secret_id)
        self.assertEqual(secret.get_content(), {'password': 'hunter2'})

    def test_add_model_secret_by_app_instance(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        relation_id = harness.add_relation('db', 'database')
        harness.add_relation_unit(relation_id, 'database/0')

        app = harness.model.get_app('database')
        secret_id = harness.add_model_secret(app, {'password': 'hunter3'})
        harness.grant_secret(secret_id, 'webapp')
        secret = harness.model.get_secret(id=secret_id)
        self.assertEqual(secret.id, secret_id)
        self.assertEqual(secret.get_content(), {'password': 'hunter3'})

    def test_add_model_secret_by_unit_instance(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        relation_id = harness.add_relation('db', 'database')
        harness.add_relation_unit(relation_id, 'database/0')

        unit = harness.model.get_unit('database/0')
        secret_id = harness.add_model_secret(unit, {'password': 'hunter4'})
        harness.grant_secret(secret_id, 'webapp')
        secret = harness.model.get_secret(id=secret_id)
        self.assertEqual(secret.id, secret_id)
        self.assertEqual(secret.get_content(), {'password': 'hunter4'})

    def test_add_model_secret_invalid_content(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)

        with self.assertRaises(ValueError):
            harness.add_model_secret('database', {'x': 'y'})  # key too short

    def test_set_secret_content(self):
        harness = ops.testing.Harness(EventRecorder, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        relation_id = harness.add_relation('db', 'database')
        harness.add_relation_unit(relation_id, 'database/0')

        secret_id = harness.add_model_secret('database', {'foo': '1'})
        harness.grant_secret(secret_id, 'webapp')
        harness.begin()
        harness.framework.observe(harness.charm.on.secret_changed, harness.charm.record_event)
        harness.set_secret_content(secret_id, {'foo': '2'})

        self.assertEqual(len(harness.charm.events), 1)
        event = harness.charm.events[0]
        self.assertIsInstance(event, ops.SecretChangedEvent)
        self.assertEqual(event.secret.get_content(), {'foo': '1'})
        self.assertEqual(event.secret.get_content(refresh=True), {'foo': '2'})
        self.assertEqual(event.secret.get_content(), {'foo': '2'})

        self.assertEqual(harness.get_secret_revisions(secret_id), [1, 2])

    def test_set_secret_content_wrong_owner(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)

        secret = harness.model.app.add_secret({'foo': 'bar'})
        with self.assertRaises(RuntimeError):
            harness.set_secret_content(secret.id, {'bar': 'foo'})

    def test_set_secret_content_invalid_secret_id(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)

        with self.assertRaises(RuntimeError):
            harness.set_secret_content('asdf', {'foo': 'bar'})

    def test_set_secret_content_invalid_content(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)

        secret_id = harness.add_model_secret('database', {'foo': 'bar'})
        with self.assertRaises(ValueError):
            harness.set_secret_content(secret_id, {'x': 'y'})

    def test_grant_secret_and_revoke_secret(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        relation_id = harness.add_relation('db', 'database')
        harness.add_relation_unit(relation_id, 'database/0')

        secret_id = harness.add_model_secret('database', {'password': 'hunter2'})
        harness.grant_secret(secret_id, 'webapp')
        secret = harness.model.get_secret(id=secret_id)
        self.assertEqual(secret.id, secret_id)
        self.assertEqual(secret.get_content(), {'password': 'hunter2'})

        harness.revoke_secret(secret_id, 'webapp')
        with self.assertRaises(ops.SecretNotFoundError):
            harness.model.get_secret(id=secret_id)

    def test_grant_secret_wrong_app(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        relation_id = harness.add_relation('db', 'database')
        harness.add_relation_unit(relation_id, 'database/0')

        secret_id = harness.add_model_secret('database', {'password': 'hunter2'})
        harness.grant_secret(secret_id, 'otherapp')
        with self.assertRaises(ops.SecretNotFoundError):
            harness.model.get_secret(id=secret_id)

    def test_grant_secret_wrong_unit(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        relation_id = harness.add_relation('db', 'database')
        harness.add_relation_unit(relation_id, 'database/0')

        secret_id = harness.add_model_secret('database', {'password': 'hunter2'})
        harness.grant_secret(secret_id, 'webapp/1')  # should be webapp/0
        with self.assertRaises(ops.SecretNotFoundError):
            harness.model.get_secret(id=secret_id)

    def test_grant_secret_no_relation(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)

        secret_id = harness.add_model_secret('database', {'password': 'hunter2'})
        with self.assertRaises(RuntimeError):
            harness.grant_secret(secret_id, 'webapp')

    def test_get_secret_grants(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: database')
        self.addCleanup(harness.cleanup)

        relation_id = harness.add_relation('db', 'webapp')
        harness.add_relation_unit(relation_id, 'webapp/0')

        secret = harness.model.app.add_secret({'foo': 'x'})
        self.assertEqual(harness.get_secret_grants(secret.id, relation_id), set())
        secret.grant(harness.model.get_relation('db'))
        self.assertEqual(harness.get_secret_grants(secret.id, relation_id), {'webapp'})

        secret.revoke(harness.model.get_relation('db'))
        self.assertEqual(harness.get_secret_grants(secret.id, relation_id), set())
        secret.grant(harness.model.get_relation('db'), unit=harness.model.get_unit('webapp/0'))
        self.assertEqual(harness.get_secret_grants(secret.id, relation_id), {'webapp/0'})

    def test_trigger_secret_rotation(self):
        harness = ops.testing.Harness(EventRecorder, meta='name: database')
        self.addCleanup(harness.cleanup)

        secret = harness.model.app.add_secret({'foo': 'x'}, label='lbl')
        harness.begin()
        harness.framework.observe(harness.charm.on.secret_rotate, harness.charm.record_event)
        harness.trigger_secret_rotation(secret.id)
        harness.trigger_secret_rotation(secret.id, label='override')

        self.assertEqual(len(harness.charm.events), 2)
        event = harness.charm.events[0]
        self.assertIsInstance(event, ops.SecretRotateEvent)
        self.assertEqual(event.secret.label, 'lbl')
        self.assertEqual(event.secret.get_content(), {'foo': 'x'})
        event = harness.charm.events[1]
        self.assertIsInstance(event, ops.SecretRotateEvent)
        self.assertEqual(event.secret.label, 'override')
        self.assertEqual(event.secret.get_content(), {'foo': 'x'})

        with self.assertRaises(RuntimeError):
            harness.trigger_secret_rotation('nosecret')

    def test_trigger_secret_removal(self):
        harness = ops.testing.Harness(EventRecorder, meta='name: database')
        self.addCleanup(harness.cleanup)

        secret = harness.model.app.add_secret({'foo': 'x'}, label='lbl')
        harness.begin()
        harness.framework.observe(harness.charm.on.secret_remove, harness.charm.record_event)
        harness.trigger_secret_removal(secret.id, 1)
        harness.trigger_secret_removal(secret.id, 42, label='override')

        self.assertEqual(len(harness.charm.events), 2)
        event = harness.charm.events[0]
        self.assertIsInstance(event, ops.SecretRemoveEvent)
        self.assertEqual(event.secret.label, 'lbl')
        self.assertEqual(event.revision, 1)
        self.assertEqual(event.secret.get_content(), {'foo': 'x'})
        event = harness.charm.events[1]
        self.assertIsInstance(event, ops.SecretRemoveEvent)
        self.assertEqual(event.secret.label, 'override')
        self.assertEqual(event.revision, 42)
        self.assertEqual(event.secret.get_content(), {'foo': 'x'})

        with self.assertRaises(RuntimeError):
            harness.trigger_secret_removal('nosecret', 1)

    def test_trigger_secret_expiration(self):
        harness = ops.testing.Harness(EventRecorder, meta='name: database')
        self.addCleanup(harness.cleanup)

        secret = harness.model.app.add_secret({'foo': 'x'}, label='lbl')
        harness.begin()
        harness.framework.observe(harness.charm.on.secret_remove, harness.charm.record_event)
        harness.trigger_secret_removal(secret.id, 1)
        harness.trigger_secret_removal(secret.id, 42, label='override')

        self.assertEqual(len(harness.charm.events), 2)
        event = harness.charm.events[0]
        self.assertIsInstance(event, ops.SecretRemoveEvent)
        self.assertEqual(event.secret.label, 'lbl')
        self.assertEqual(event.revision, 1)
        self.assertEqual(event.secret.get_content(), {'foo': 'x'})
        event = harness.charm.events[1]
        self.assertIsInstance(event, ops.SecretRemoveEvent)
        self.assertEqual(event.secret.label, 'override')
        self.assertEqual(event.revision, 42)
        self.assertEqual(event.secret.get_content(), {'foo': 'x'})

        with self.assertRaises(RuntimeError):
            harness.trigger_secret_removal('nosecret', 1)


class EventRecorder(ops.CharmBase):
    def __init__(self, framework):
        super().__init__(framework)
        self.events = []

    def record_event(self, event):
        self.events.append(event)


class TestPorts(unittest.TestCase):
    def test_ports(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        unit = harness.model.unit

        unit.open_port('tcp', 8080)
        unit.open_port('udp', 4000)
        unit.open_port('icmp')

        ports_set = unit.opened_ports()
        self.assertIsInstance(ports_set, set)
        ports = sorted(ports_set, key=lambda p: (p.protocol, p.port))
        self.assertEqual(len(ports), 3)
        self.assertIsInstance(ports[0], ops.OpenedPort)
        self.assertEqual(ports[0].protocol, 'icmp')
        self.assertIsNone(ports[0].port)
        self.assertIsInstance(ports[1], ops.OpenedPort)
        self.assertEqual(ports[1].protocol, 'tcp')
        self.assertEqual(ports[1].port, 8080)
        self.assertIsInstance(ports[2], ops.OpenedPort)
        self.assertEqual(ports[2].protocol, 'udp')
        self.assertEqual(ports[2].port, 4000)

        unit.close_port('tcp', 8080)
        unit.close_port('tcp', 8080)  # closing same port again has no effect
        unit.close_port('udp', 4000)

        ports_set = unit.opened_ports()
        self.assertIsInstance(ports_set, set)
        ports = sorted(ports_set, key=lambda p: (p.protocol, p.port))
        self.assertEqual(len(ports), 1)
        self.assertIsInstance(ports[0], ops.OpenedPort)
        self.assertEqual(ports[0].protocol, 'icmp')
        self.assertIsNone(ports[0].port)

        unit.close_port('icmp')

        ports_set = unit.opened_ports()
        self.assertEqual(ports_set, set())

    def test_errors(self):
        harness = ops.testing.Harness(ops.CharmBase, meta='name: webapp')
        self.addCleanup(harness.cleanup)
        unit = harness.model.unit

        with self.assertRaises(ops.ModelError):
            unit.open_port('icmp', 8080)  # icmp cannot have port
        with self.assertRaises(ops.ModelError):
            unit.open_port('ftp', 8080)  # invalid protocol
        with self.assertRaises(ops.ModelError):
            unit.open_port('tcp')  # tcp must have port
        with self.assertRaises(ops.ModelError):
            unit.open_port('udp')  # udp must have port
        with self.assertRaises(ops.ModelError):
            unit.open_port('tcp', 0)  # port out of range
        with self.assertRaises(ops.ModelError):
            unit.open_port('tcp', 65536)  # port out of range
