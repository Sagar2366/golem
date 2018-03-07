# pylint: disable=protected-access
import calendar
import datetime

import os
import pathlib
import pickle
import random
import time
import uuid
from unittest import TestCase
from unittest.mock import patch, ANY, Mock, MagicMock, _SentinelObject

import golem_messages

from golem_messages import message

from golem import model, testutils
from golem.core.databuffer import DataBuffer
from golem.core.variables import PROTOCOL_CONST
from golem.docker.environment import DockerEnvironment
from golem.docker.image import DockerImage
from golem.model import Actor
from golem.network import history
from golem.network.p2p.node import Node
from golem.network.pending import PendingSessionMessages
from golem.network.transport.tcpnetwork import BasicProtocol
from golem.resource.client import ClientOptions
from golem.task import taskstate
from golem.task.taskbase import ResultType, TaskHeader
from golem.task.taskkeeper import CompTaskKeeper
from golem.task.tasksession import TaskSession, logger, get_task_message
from golem.tools.assertlogs import LogTestCase
from tests import factories
from tests.factories import messages as msg_factories
from tests.factories.taskserver import WaitingTaskResultFactory


def fill_slots(msg):
    for slot in msg.__slots__:
        if hasattr(msg, slot):
            continue
        setattr(msg, slot, None)


class DockerEnvironmentMock(DockerEnvironment):
    DOCKER_IMAGE = ""
    DOCKER_TAG = ""
    ENV_ID = ""
    APP_DIR = ""
    SCRIPT_NAME = ""
    SHORT_DESCRIPTION = ""


class TestTaskSessionPep8(testutils.PEP8MixIn, TestCase):
    PEP8_FILES = ['golem/task/tasksession.py', ]


class ConcentMessageMixin():
    def assert_concent_cancel(self, mock_call, subtask_id, message_class_name):
        self.assertEqual(mock_call[0], subtask_id)
        self.assertEqual(mock_call[1], message_class_name)

    def assert_concent_submit(self, mock_call, subtask_id, message_class):
        self.assertEqual(mock_call[0], subtask_id)
        self.assertIsInstance(mock_call[1], message_class)


extra_serializers = golem_messages.serializer.ENCODERS
extra_serializers[Mock] = lambda *_: None
extra_serializers[_SentinelObject] = lambda *_: None


class TestTaskSession(ConcentMessageMixin, LogTestCase,
                      testutils.TempDirFixture):
    def setUp(self):
        super(TestTaskSession, self).setUp()
        random.seed()
        self.task_session = TaskSession(Mock())

    @patch('golem.task.tasksession.TaskSession.send')
    def test_hello(self, send_mock, *_):
        node = Mock(to_dict=Mock(return_value=dict()))
        self.task_session.conn.server.node = node
        self.task_session.conn.server.get_key_id.return_value = key_id = \
            'key id%d' % (random.random() * 1000,)
        self.task_session.send_hello()
        expected = [
            ['rand_val', self.task_session.rand_val],
            ['proto_id', PROTOCOL_CONST.ID],
            ['node_name', None],
            ['node_info', dict()],
            ['port', None],
            ['client_ver', None],
            ['client_key_id', key_id],
            ['solve_challenge', None],
            ['challenge', None],
            ['difficulty', None],
            ['metadata', None],
        ]
        msg = send_mock.call_args[0][0]
        self.assertCountEqual(msg.slots(), expected)

    @patch('golem.network.history.MessageHistoryService.instance')
    def test_request_task(self, *_):  # pylint: disable=too-many-statements
        task_manager = Mock(tasks_states={}, tasks={})
        conn = Mock(
            server=Mock(task_manager=task_manager)
        )
        ts = TaskSession(conn)
        ts._get_handshake = Mock(return_value={})
        ts.verified = True
        ts.request_task("ABC", "xyz", 1030, 30, 3, 1, 8)
        mt = ts.conn.send_message.call_args[0][0]
        self.assertIsInstance(mt, message.WantToComputeTask)
        self.assertEqual(mt.node_name, "ABC")
        self.assertEqual(mt.task_id, "xyz")
        self.assertEqual(mt.perf_index, 1030)
        self.assertEqual(mt.price, 30)
        self.assertEqual(mt.max_resource_size, 3)
        self.assertEqual(mt.max_memory_size, 1)
        self.assertEqual(mt.num_cores, 8)
        ts2 = TaskSession(conn)
        ts2.verified = True
        ts2.key_id = provider_key = "DEF"
        ts2.can_be_not_encrypted.append(mt.TYPE)
        ts2.task_server.should_accept_provider.return_value = False
        ts2.task_server.config_desc.max_price = 100

        task_id = '42'
        requestor_key = 'req pubkey'
        task_manager.tasks[task_id] = Mock(header=TaskHeader(
            node_name='ABC',
            task_id='xyz',
            task_owner_address='10.10.10.10',
            task_owner_port=12345,
            task_owner_key_id=requestor_key,
            environment='',
            task_owner=Node(key=requestor_key)
        ))

        ctd = message.tasks.ComputeTaskDef()
        ctd['task_id'] = task_id

        task_state = taskstate.TaskState()
        task_state.package_hash = '667'
        conn.server.task_manager.tasks_states[ctd['task_id']] = task_state

        ts2.task_manager.get_next_subtask.return_value = (ctd, False, False)
        ts2.interpret(mt)
        ms = ts2.conn.send_message.call_args[0][0]
        self.assertIsInstance(ms, message.CannotAssignTask)
        self.assertEqual(ms.task_id, mt.task_id)
        ts2.task_server.should_accept_provider.return_value = True
        ts2.interpret(mt)
        ms = ts2.conn.send_message.call_args[0][0]
        self.assertIsInstance(ms, message.TaskToCompute)
        expected = [
            ['requestor_id', requestor_key],
            ['provider_id', provider_key],
            ['requestor_public_key', requestor_key],
            ['requestor_ethereum_public_key', requestor_key],
            ['provider_public_key', provider_key],
            ['provider_ethereum_public_key', provider_key],
            ['compute_task_def', ctd],
            ['package_hash', 'sha1:' + task_state.package_hash],
            ['concent_enabled', True],
        ]
        self.assertCountEqual(ms.slots(), expected)
        ts2.task_manager.get_next_subtask.return_value = (ctd, True, False)
        ts2.interpret(mt)
        ms = ts2.conn.send_message.call_args[0][0]
        self.assertIsInstance(ms, message.CannotAssignTask)
        self.assertEqual(ms.task_id, mt.task_id)
        ts2.task_manager.get_node_id_for_subtask.return_value = "DEF"
        ts2._react_to_cannot_compute_task(message.CannotComputeTask(
            reason=message.CannotComputeTask.REASON.WrongCTD,
            subtask_id=None,
        ))
        assert ts2.task_manager.task_computation_failure.called
        ts2.task_manager.task_computation_failure.called = False
        ts2.task_manager.get_node_id_for_subtask.return_value = "___"
        ts2._react_to_cannot_compute_task(message.CannotComputeTask(
            reason=message.CannotComputeTask.REASON.WrongCTD,
            subtask_id=None,
        ))
        assert not ts2.task_manager.task_computation_failure.called

    @patch(
        'golem.network.history.MessageHistoryService.get_sync_as_message',
    )
    def test_send_report_computed_task(self, get_mock):
        ts = self.task_session
        ts.verified = True
        ts.task_server.get_node_name.return_value = "ABC"
        wtr = WaitingTaskResultFactory()

        get_mock.return_value = factories.messages.TaskToCompute(
            compute_task_def__task_id=wtr.task_id,
            compute_task_def__deadline=calendar.timegm(time.gmtime()) + 3600,
        )
        ts.task_server.get_key_id.return_value = 'key id'
        ts.send_report_computed_task(
            wtr, wtr.owner_address, wtr.owner_port, "0x00", wtr.owner)

        rct: message.ReportComputedTask = ts.conn.send_message.call_args[0][0]
        self.assertIsInstance(rct, message.ReportComputedTask)
        self.assertEqual(rct.subtask_id, wtr.subtask_id)
        self.assertEqual(rct.result_type, ResultType.DATA)
        self.assertEqual(rct.computation_time, wtr.computing_time)
        self.assertEqual(rct.node_name, "ABC")
        self.assertEqual(rct.address, wtr.owner_address)
        self.assertEqual(rct.port, wtr.owner_port)
        self.assertEqual(rct.eth_account, "0x00")
        self.assertEqual(rct.extra_data, [])
        self.assertEqual(rct.node_info, wtr.owner.to_dict())
        self.assertEqual(rct.package_hash, 'sha1:' + wtr.package_sha1)
        self.assertEqual(rct.multihash, wtr.result_hash)
        self.assertEqual(rct.secret, wtr.result_secret)

        ts2 = TaskSession(Mock())
        ts2.verified = True
        ts2.key_id = "DEF"
        ts2.can_be_not_encrypted.append(rct.TYPE)
        ts2.task_manager.subtask2task_mapping = {wtr.subtask_id: wtr.task_id}
        task_state = taskstate.TaskState()
        task_state.subtask_states[wtr.subtask_id] = taskstate.SubtaskState()
        task_state.subtask_states[wtr.subtask_id].deadline = \
            calendar.timegm(time.gmtime()) + 3600
        ts2.task_manager.tasks_states = {
            wtr.task_id: task_state,
        }
        ts2.task_manager.get_node_id_for_subtask.return_value = "DEF"
        get_mock.side_effect = history.MessageNotFound

        with patch(
                'golem.network.concent.helpers'
                '.process_report_computed_task',
                return_value=msg_factories.AckReportComputedTask()):
            ts2.interpret(rct)
        ts2.task_server.receive_subtask_computation_time.assert_called_with(
            wtr.subtask_id, wtr.computing_time)
        wtr.result_type = "UNKNOWN"
        with self.assertLogs(logger, level="ERROR"):
            ts.send_report_computed_task(
                wtr, wtr.owner_address, wtr.owner_port, "0x00", wtr.owner)

    def test_react_to_hello(self):
        conn = MagicMock()

        ts = TaskSession(conn)
        ts.task_server = Mock()
        ts.disconnect = Mock()
        ts.send = Mock()

        key_id = 'deadbeef'
        peer_info = MagicMock()
        peer_info.key = key_id
        msg = message.Hello(port=1, node_name='node2', client_key_id=key_id,
                            node_info=peer_info,
                            proto_id=-1)

        fill_slots(msg)
        ts._react_to_hello(msg)
        ts.disconnect.assert_called_with(
            message.Disconnect.REASON.ProtocolVersion)

        msg.proto_id = PROTOCOL_CONST.ID

        ts._react_to_hello(msg)
        self.assertTrue(ts.send.called)

    @patch.object(golem_messages.serializer, 'ENCODERS', extra_serializers)
    @patch('golem.task.tasksession.get_task_message',
           return_value=message.ReportComputedTask())
    def test_result_received(self, *_):
        pending_messages = PendingSessionMessages(db_dir=self.tempdir)

        conn = Mock()
        ts = TaskSession(conn)
        ts.task_server = Mock(pending_messages=pending_messages)
        ts.task_manager = Mock()
        ts.task_manager.verify_subtask.return_value = True
        ts.key_id = "key_id"
        subtask_id = "xxyyzz"

        def finished():
            if not ts.task_manager.verify_subtask(subtask_id):
                ts._reject_subtask_result(subtask_id, '')
                ts.dropped()
                return

            payment = ts.task_server.accept_result(subtask_id,
                                                   ts.result_owner)
            ts.send(factories.messages.SubtaskResultsAcceptedFactory(
                task_to_compute__compute_task_def__subtask_id=subtask_id,
                payment_ts=payment.processed_ts))
            ts.dropped()

        extra_data = dict(
            # the result is explicitly serialized using cPickle
            result=pickle.dumps({'stdout': 'xyz'}),
            result_type=None,
            subtask_id='xxyyzz'
        )

        ts.result_received(extra_data, decrypt=False)

        pending_message = next(pending_messages.get(node_id=ts.key_id))
        assert pending_message.type == message.tasks.SubtaskResultsRejected.TYPE
        assert conn.close.called
        pending_message.delete_instance()

        extra_data.update(dict(
            result_type=ResultType.DATA,
        ))
        conn.close.called = False

        ts.task_manager.computed_task_received = Mock(
            side_effect=finished(),
        )
        ts.result_received(extra_data, decrypt=False)

        pending_message = next(pending_messages.get(node_id=ts.key_id))
        assert pending_message.type == message.tasks.SubtaskResultsAccepted.TYPE
        assert conn.close.called
        pending_message.delete_instance()

        extra_data.update(dict(
            subtask_id=None,
        ))
        conn.close.called = False
        ts.result_received(extra_data, decrypt=False)

        try:
            pending_message = next(pending_messages.get(node_id=ts.key_id))
        except StopIteration:
            pending_message = None

        assert pending_message is None
        assert conn.close.called

    def test_result_rejected(self):
        # pylint: disable=no-value-for-parameter
        self._test_result_rejected()

    def test_result_rejected_with_wrong_key(self):
        # pylint: disable=no-value-for-parameter
        self._test_result_rejected(key_id="ABC2", called=False)

    @patch('golem.task.tasksession.TaskSession.dropped')
    def _test_result_rejected(self, dropped_mock, key_id="ABC", called=True):
        msg = factories.messages.SubtaskResultsRejected()
        ctk = self.task_session.task_manager.comp_task_keeper
        ctk.get_node_for_task_id.return_value = "ABC"
        self.task_session.key_id = key_id
        self.task_session._react_to_subtask_results_rejected(msg)
        ts = self.task_session.task_server
        if called:
            ts.subtask_rejected.assert_called_once_with(
                subtask_id=msg.report_computed_task.subtask_id,
            )
        else:
            ts.subtask_rejected.assert_not_called()
        dropped_mock.assert_called_once_with()

    # pylint: disable=too-many-statements
    def test_react_to_task_to_compute(self):
        conn = Mock()
        ts = TaskSession(conn)
        ts.key_id = "KEY_ID"
        ts.task_manager = MagicMock()
        ts.task_computer = Mock()
        ts.task_server = Mock()
        ts.send = Mock()

        env = Mock()
        env.docker_images = [DockerImage("dockerix/xii", tag="323")]
        env.allow_custom_main_program_file = False
        env.get_source_code.return_value = None
        ts.task_server.get_environment_by_id.return_value = env

        reasons = message.CannotComputeTask.REASON

        def __reset_mocks():
            ts.task_manager.reset_mock()
            ts.task_computer.reset_mock()
            conn.reset_mock()

        # msg.ctd is None -> failure
        msg = message.TaskToCompute()
        ts._react_to_task_to_compute(msg)
        ts.task_server.add_task_session.assert_not_called()
        ts.task_computer.task_given.assert_not_called()
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.send.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # No source code in the local environment -> failure
        __reset_mocks()
        header = ts.task_manager.comp_task_keeper.get_task_header()
        header.task_owner_key_id = 'KEY_ID'
        header.task_owner.key = 'KEY_ID'
        header.task_owner_address = '10.10.10.10'
        header.task_owner_port = 1112

        ctd = message.ComputeTaskDef()
        ctd['subtask_id'] = "SUBTASKID"
        ctd['docker_images'] = [
            DockerImage("dockerix/xiii", tag="323").to_dict(),
        ]
        msg = message.TaskToCompute(compute_task_def=ctd)
        ts._react_to_task_to_compute(msg)
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Source code from local environment -> proper execution
        __reset_mocks()
        env.get_source_code.return_value = "print 'Hello world'"
        ts._react_to_task_to_compute(msg)
        ts.task_manager.comp_task_keeper.receive_subtask.assert_called_with(ctd)
        ts.task_computer.session_closed.assert_not_called()
        ts.task_server.add_task_session.assert_called_with("SUBTASKID", ts)
        ts.task_computer.task_given.assert_called_with(ctd)
        conn.close.assert_not_called()

        # Wrong key id -> failure
        __reset_mocks()
        header.task_owner_key_id = 'KEY_ID2'
        header.task_owner.key = 'KEY_ID2'

        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Wrong task owner key id -> failure
        __reset_mocks()
        header.task_owner_key_id = 'KEY_ID'
        header.task_owner.key = 'KEY_ID2'

        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Wrong return port -> failure
        __reset_mocks()
        header.task_owner_key_id = 'KEY_ID'
        header.task_owner.key = 'KEY_ID'
        header.task_owner_port = 0

        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Proper port and key -> proper execution
        __reset_mocks()
        header.task_owner_port = 1112

        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        conn.close.assert_not_called()

        # Allow custom code / no code in message.ComputeTaskDef -> failure
        __reset_mocks()
        env.allow_custom_main_program_file = True
        ctd['src_code'] = ""
        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Allow custom code / code in ComputerTaskDef -> proper execution
        __reset_mocks()
        ctd['src_code'] = "print 'Hello world!'"
        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        ts.task_computer.session_closed.assert_not_called()
        ts.task_server.add_task_session.assert_called_with("SUBTASKID", ts)
        ts.task_computer.task_given.assert_called_with(ctd)
        conn.close.assert_not_called()

        # No environment available -> failure
        __reset_mocks()
        ts.task_server.get_environment_by_id.return_value = None
        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        assert ts.err_msg == reasons.WrongEnvironment
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Envrionment is Docker environment but with different images -> failure
        __reset_mocks()
        ts.task_server.get_environment_by_id.return_value = \
            DockerEnvironmentMock(additional_images=[
                DockerImage("dockerix/xii", tag="323"),
                DockerImage("dockerix/xiii", tag="325"),
                DockerImage("dockerix/xiii")
            ])
        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        assert ts.err_msg == reasons.WrongDockerImages
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Envrionment is Docker environment with proper images,
        # but no srouce code -> failure
        __reset_mocks()
        de = DockerEnvironmentMock(additional_images=[
            DockerImage("dockerix/xii", tag="323"),
            DockerImage("dockerix/xiii", tag="325"),
            DockerImage("dockerix/xiii", tag="323")
        ])
        ts.task_server.get_environment_by_id.return_value = de
        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        assert ts.err_msg == reasons.NoSourceCode
        ts.task_manager.comp_task_keeper.receive_subtask.assert_not_called()
        ts.task_computer.session_closed.assert_called_with()
        assert conn.close.called

        # Proper Docker environment with source code
        __reset_mocks()
        file_name = os.path.join(self.path, "main_program_file")
        with open(file_name, 'w') as f:
            f.write("Hello world!")
        de.main_program_file = file_name
        ts._react_to_task_to_compute(message.TaskToCompute(
            compute_task_def=ctd,
        ))
        ts.task_server.add_task_session.assert_called_with("SUBTASKID", ts)
        ts.task_computer.task_given.assert_called_with(ctd)
        conn.close.assert_not_called()

    # pylint: enable=too-many-statements

    def test_get_resource(self):
        conn = BasicProtocol()
        conn.transport = Mock()
        conn.server = Mock()

        db = DataBuffer()

        sess = TaskSession(conn)
        sess.send = lambda m: db.append_bytes(
            m.serialize(lambda x: b'\000' * 65),
        )
        sess._can_send = lambda *_: True
        sess.request_resource(str(uuid.uuid4()))

        self.assertTrue(
            message.Message.deserialize(db.buffered_data, lambda x: x)
        )

    def test_react_to_ack_reject_report_computed_task(self):
        task_keeper = CompTaskKeeper(pathlib.Path(self.path))
        subtask_id = '1337'
        task_id = '42'

        session = self.task_session
        session.concent_service = MagicMock()
        session.task_manager.comp_task_keeper = task_keeper
        session.key_id = 'owner_id'

        cancel = session.concent_service.cancel_task_message

        msg_ack = message.AckReportComputedTask(
            subtask_id=subtask_id
        )
        msg_rej = message.RejectReportComputedTask(
            subtask_id=subtask_id,
        )

        # Subtask is not known
        session._react_to_ack_report_computed_task(msg_ack)
        self.assertFalse(cancel.called)
        session._react_to_reject_report_computed_task(msg_rej)
        self.assertFalse(cancel.called)

        # Save subtask information
        task = Mock(header=Mock(task_owner_key_id='owner_id'))
        task_keeper.subtask_to_task[subtask_id] = task_id
        task_keeper.active_tasks[task_id] = task

        # Subtask is known
        session._react_to_ack_report_computed_task(msg_ack)
        self.assertTrue(cancel.called)
        self.assert_concent_cancel(
            cancel.call_args[0], subtask_id, 'ForceReportComputedTask')

        cancel.reset_mock()
        session._react_to_reject_report_computed_task(msg_ack)
        self.assert_concent_cancel(
            cancel.call_args[0], subtask_id, 'ForceReportComputedTask')

    def test_react_to_resource_list(self):
        task_server = self.task_session.task_server

        client = 'test_client'
        version = 1.0
        peers = [{'TCP': ('127.0.0.1', 3282)}]
        msg = message.ResourceList(resources=[['1'], ['2']],
                                   options=None)

        # Use locally saved hyperdrive client options
        self.task_session._react_to_resource_list(msg)
        call_options = task_server.pull_resources.call_args[1]

        assert task_server.get_download_options.called
        assert task_server.pull_resources.called
        assert isinstance(call_options['client_options'], Mock)

        # Use download options built by TaskServer
        client_options = ClientOptions(client, version,
                                       options={'peers': peers})
        task_server.get_download_options.return_value = client_options

        self.task_session.task_server.pull_resources.reset_mock()
        self.task_session._react_to_resource_list(msg)
        call_options = task_server.pull_resources.call_args[1]

        assert not isinstance(call_options['client_options'], Mock)
        assert call_options['client_options'].options['peers'] == peers

    def test_task_subtask_from_message(self):
        self.task_session._subtask_to_task = Mock(return_value=None)
        definition = message.ComputeTaskDef({'task_id': 't', 'subtask_id': 's'})
        msg = message.TaskToCompute(compute_task_def=definition)

        task, subtask = self.task_session._task_subtask_from_message(
            msg, Actor.Provider)

        assert task == definition['task_id']
        assert subtask == definition['subtask_id']
        assert not self.task_session._subtask_to_task.called

    def test_task_subtask_from_other_message(self):
        self.task_session._subtask_to_task = Mock(return_value=None)
        msg = message.Hello()

        task, subtask = self.task_session._task_subtask_from_message(
            msg, Actor.Provider
        )

        assert not task
        assert not subtask
        assert self.task_session._subtask_to_task.called

    def test_subtask_to_task(self):
        task_keeper = Mock(subtask_to_task=dict())
        mapping = dict()

        self.task_session.task_manager.comp_task_keeper = task_keeper
        self.task_session.task_manager.subtask2task_mapping = mapping
        task_keeper.subtask_to_task['sid_1'] = 'task_1'
        mapping['sid_2'] = 'task_2'

        assert self.task_session._subtask_to_task('sid_1', Actor.Provider)
        assert self.task_session._subtask_to_task('sid_2', Actor.Requestor)
        assert not self.task_session._subtask_to_task('sid_2', Actor.Provider)
        assert not self.task_session._subtask_to_task('sid_1', Actor.Requestor)

        self.task_session.task_manager = None
        assert not self.task_session._subtask_to_task('sid_1', Actor.Provider)
        assert not self.task_session._subtask_to_task('sid_2', Actor.Requestor)

    def test_react_to_cannot_assign_task(self):
        self._test_react_to_cannot_assign_task()

    def test_react_to_cannot_assign_task_with_wrong_sender(self):
        self._test_react_to_cannot_assign_task("KEY_ID2", expected_requests=1)

    def _test_react_to_cannot_assign_task(self, key_id="KEY_ID",
                                          expected_requests=0):
        task_keeper = CompTaskKeeper(self.new_path)
        task_keeper.add_request(TaskHeader(environment='DEFAULT',
                                           node_name="ABC",
                                           task_id="abc",
                                           task_owner_address="10.10.10.10",
                                           task_owner_port=2311,
                                           task_owner_key_id="KEY_ID"), 20)
        assert task_keeper.active_tasks["abc"].requests == 1
        self.task_session.task_manager.comp_task_keeper = task_keeper
        msg_cat = message.CannotAssignTask(task_id="abc")
        self.task_session.key_id = key_id
        self.task_session._react_to_cannot_assign_task(msg_cat)
        assert task_keeper.active_tasks["abc"].requests == expected_requests


class ForceReportComputedTaskTestCase(testutils.DatabaseFixture,
                                      testutils.TempDirFixture):
    def setUp(self):
        testutils.DatabaseFixture.setUp(self)
        testutils.TempDirFixture.setUp(self)
        history.MessageHistoryService()
        self.ts = TaskSession(Mock())
        self.n = Node()
        self.task_id = str(uuid.uuid4())
        self.subtask_id = str(uuid.uuid4())
        self.node_id = str(uuid.uuid4())

    def tearDown(self):
        testutils.DatabaseFixture.tearDown(self)
        testutils.TempDirFixture.tearDown(self)
        history.MessageHistoryService.instance = None

    @staticmethod
    def _mock_task_to_compute(task_id, subtask_id, node_id, **kwargs):
        task_to_compute = message.TaskToCompute(**kwargs)
        nmsg_dict = dict(
            task=task_id,
            subtask=subtask_id,
            node=node_id,
            msg_date=datetime.datetime.now(),
            msg_cls='TaskToCompute',
            msg_data=pickle.dumps(task_to_compute),
            local_role=model.Actor.Provider,
            remote_role=model.Actor.Requestor,
        )
        service = history.MessageHistoryService.instance
        service.add_sync(nmsg_dict)

    def assert_submit_task_message(self, subtask_id, wtr):
        self.ts.concent_service.submit_task_message.assert_called_once_with(
            subtask_id, ANY)

        msg = self.ts.concent_service.submit_task_message.call_args[0][1]
        self.assertEqual(msg.result_hash, 'sha1:' + wtr.package_sha1)

    def test_send_report_computed_task_concent_no_message(self):
        wtr = factories.taskserver.WaitingTaskResultFactory(owner=self.n)
        self.ts.send_report_computed_task(
            wtr, wtr.owner_address, wtr.owner_port, "0x00", self.n)
        self.ts.concent_service.submit.assert_not_called()

    def test_send_report_computed_task_concent_success(self):
        wtr = factories.taskserver.WaitingTaskResultFactory(
            task_id=self.task_id, subtask_id=self.subtask_id, owner=self.n)
        self._mock_task_to_compute(self.task_id, self.subtask_id, self.node_id)
        self.ts.send_report_computed_task(
            wtr, wtr.owner_address, wtr.owner_port, "0x00", self.n)

        self.assert_submit_task_message(self.subtask_id, wtr)

    def test_send_report_computed_task_concent_success_many_files(self):
        result = []
        for i in range(100, 300, 99):
            p = pathlib.Path(self.tempdir) / str(i)
            with p.open('wb') as f:
                f.write(b'\0' * i * 2 ** 20)
            result.append(str(p))

        wtr = factories.taskserver.WaitingTaskResultFactory(
            task_id=self.task_id, subtask_id=self.subtask_id, owner=self.n,
            result=result, result_type=ResultType.FILES
        )
        self._mock_task_to_compute(self.task_id, self.subtask_id, self.node_id)

        self.ts.send_report_computed_task(
            wtr, wtr.owner_address, wtr.owner_port, "0x00", self.n)

        self.assert_submit_task_message(self.subtask_id, wtr)

    def test_send_report_computed_task_concent_disabled(self):
        wtr = factories.taskserver.WaitingTaskResultFactory(
            task_id=self.task_id, subtask_id=self.subtask_id, owner=self.n)

        self._mock_task_to_compute(
            self.task_id, self.subtask_id, self.node_id, concent_enabled=False)

        self.ts.send_report_computed_task(
            wtr, wtr.owner_address, wtr.owner_port, "0x00", self.n)
        self.ts.concent_service.submit.assert_not_called()


class GetTaskMessageTest(TestCase):
    def test_get_task_message(self):
        msg = factories.messages.TaskToCompute()
        with patch('golem.task.tasksession.history'
                   '.MessageHistoryService.get_sync_as_message',
                   Mock(return_value=msg)):
            msg_historical = get_task_message('TaskToCompute', 'foo', 'bar')
            self.assertEqual(msg, msg_historical)

    def test_get_task_message_fail(self):
        with patch('golem.task.tasksession.history'
                   '.MessageHistoryService.get_sync_as_message',
                   Mock(side_effect=history.MessageNotFound())):
            msg = get_task_message('TaskToCompute', 'foo', 'bar')
            self.assertIsNone(msg)


class SubtaskResultsAcceptedTest(TestCase):
    def setUp(self):
        self.task_session = TaskSession(Mock())
        self.task_server = Mock()
        self.task_session.task_server = self.task_server

    def test__react_to_subtask_result_accepted(self):
        self._test__react_to_subtask_result_accepted()

    def test__react_to_subtask_result_accepted_with_wrong_key(self):
        self._test__react_to_subtask_result_accepted("DEF", called=False)

    def _test__react_to_subtask_result_accepted(self, key_id="ABC",
                                                called=True):
        sra = factories.messages.SubtaskResultsAcceptedFactory()
        ctk = self.task_session.task_manager.comp_task_keeper
        ctk.get_node_for_task_id.return_value = "ABC"
        self.task_session.key_id = key_id
        self.task_session._react_to_subtask_result_accepted(sra)
        if called:
            self.task_server.subtask_accepted.assert_called_once_with(
                sra.task_to_compute.compute_task_def.get('subtask_id'),
                sra.payment_ts,
            )
        else:
            self.task_server.subtask_accepted.assert_not_called()

    def test_result_received(self):
        def computed_task_received(*args):
            args[3]()

        self.task_session.task_manager = Mock()
        self.task_session.task_manager.computed_task_received = \
            computed_task_received

        ttc = factories.messages.TaskToCompute()
        extra_data = dict(
            result=pickle.dumps({'stdout': 'xyz'}),
            result_type=ResultType.DATA,
            subtask_id=ttc.compute_task_def.get('subtask_id')
        )

        self.task_session.send = Mock()

        with patch('golem.task.tasksession.get_task_message',
                   Mock(return_value=ttc)):
            self.task_session.result_received(extra_data, decrypt=False)

        assert self.task_session.send.called
        sra = self.task_session.send.call_args[0][0] # noqa pylint:disable=unsubscriptable-object
        self.assertIsInstance(sra.task_to_compute, message.tasks.TaskToCompute)


class ReportComputedTaskTest(ConcentMessageMixin, LogTestCase):

    @staticmethod
    def _create_pull_package(result):
        def pull_package(*_, **kwargs):
            success = kwargs.get('success')
            error = kwargs.get('error')
            if result:
                success(Mock())
            else:
                error(Exception('Pull failed'))

        return pull_package

    def setUp(self):
        self.task_id = 'xyz'
        self.subtask_id = 'xxyyzz'

        ts = TaskSession(Mock())
        ts.result_received = Mock()
        ts.key_id = "ABC"
        ts.task_manager.get_node_id_for_subtask.return_value = ts.key_id
        ts.task_manager.subtask2task_mapping = {
            self.subtask_id: self.task_id,
        }
        ts.task_manager.tasks = {
            self.task_id: Mock()
        }
        ts.task_manager.tasks_states = {
            self.task_id: Mock(subtask_states={
                self.subtask_id: Mock(deadline=calendar.timegm(time.gmtime()))
            })
        }
        ts.task_server.task_keeper.task_headers = {}
        self.ts = ts

        gsam = patch('golem.network.concent.helpers.history'
                     '.MessageHistoryService.get_sync_as_message',
                     Mock(side_effect=history.MessageNotFound))
        gsam.start()
        self.addCleanup(gsam.stop)

    def _prepare_report_computed_task(self, **kwargs):
        return factories.messages.ReportComputedTask(
            subtask_id=self.subtask_id,
            task_to_compute__compute_task_def__task_id=self.task_id,
            **kwargs,
        )

    def test_result_received(self):
        msg = self._prepare_report_computed_task()
        self.ts.task_manager.task_result_manager.pull_package = \
            self._create_pull_package(True)

        self.ts._react_to_report_computed_task(msg)
        self.assertTrue(self.ts.result_received.called)

        cancel = self.ts.concent_service.cancel_task_message
        self.assert_concent_cancel(
            cancel.call_args[0], self.subtask_id, 'ForceGetTaskResult')

    def test_reject_result_pull_failed_no_concent(self):
        msg = self._prepare_report_computed_task(
            task_to_compute__concent_enabled=False)

        self.ts.task_manager.task_result_manager.pull_package = \
            self._create_pull_package(False)

        self.ts._react_to_report_computed_task(msg)
        assert self.ts.task_server.reject_result.called
        assert self.ts.task_manager.task_computation_failure.called

    def test_reject_result_pull_failed_with_concent(self):
        msg = self._prepare_report_computed_task(
            task_to_compute__concent_enabled=True)

        self.ts.task_manager.task_result_manager.pull_package = \
            self._create_pull_package(False)

        self.ts._react_to_report_computed_task(msg)
        stm = self.ts.concent_service.submit_task_message
        self.assertEqual(stm.call_count, 2)

        self.assert_concent_submit(stm.call_args_list[0][0], self.subtask_id,
                                   message.concents.ForceGetTaskResult)
        self.assert_concent_submit(stm.call_args_list[1][0], self.subtask_id,
                                   message.concents.ForceGetTaskResult)

        # ensure the first call is delayed
        self.assertGreater(stm.call_args_list[0][0][2], datetime.timedelta(0))
        # ensure the second one is not
        self.assertEqual(len(stm.call_args_list[1][0]), 2)
