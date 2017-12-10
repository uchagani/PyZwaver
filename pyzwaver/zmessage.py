#!/usr/bin/python3
# Copyright 2016 Robert Muth <robert@muth.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 3
# of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""
message.py contains helpers for message encoding and the business logic
that decides when a message has been properly processed.
"""

import logging
import queue
import threading
import time
import collections

from pyzwaver import zwave

# ==================================================
# Priorities for outgoing messages
# ==================================================


def ControllerPriority():
    return (1, 0, -1)


def NodePriorityHi(node):
    return (2, 0, node)


def NodePriorityLo(node):
    return (3, 0, node)

def LowestPriority():
    return (1000, 0, -1)


class MessageQueueOut:

    def __init__(self):
        self._q = queue.PriorityQueue()
        self._lo_counts = collections.defaultdict(int)
        self._hi_counts = collections.defaultdict(int)
        self._lo_min = 0
        self._hi_min = 0
        self._counter = 0
        self._per_node_size = collections.defaultdict(int)

    def qsize(self):
        return self._q.qsize()

    def put(self, priority, message):
        if self._q.empty():
            self._lo_counts = collections.defaultdict(int)
            self._hi_counts = collections.defaultdict(int)
            self._lo_min = 0
            self._hi_min = 0


        level, count , node = priority
        if level == 2:
            count = self._hi_counts[node]
            count = max(count + 1, self._hi_min)
            self._hi_counts[node] = count
        elif level == 3:
            count = self._lo_counts[node]
            count = max(count + 1, self._lo_min)
            self._lo_counts[node] = count
        else:
            count = self._counter
            self._counter += 1
        self._per_node_size[node] += 1
        self._q.put(((level, count, node), message))


    def get(self):
        priority, message = self._q.get()
        level = priority[0]
        if level == 2:
            self._hi_min = priority[1]
        elif level == 2:
            self._lo_min = priority[1]
        self._per_node_size[priority[2]] -= 1
        return message

    def __str__(self):
        return str(self._per_node_size)


# ==================================================
# Raw Messages
# ==================================================

_CB_ID_COUNTER = 66


def CallbackId():
    global _CB_ID_COUNTER
    _CB_ID_COUNTER += 1
    _CB_ID_COUNTER %= 256
    return _CB_ID_COUNTER


def Checksum(data):
    checksum = 0xff
    for b in data:
        checksum = checksum ^ b
    return checksum


def Hexify(t):
    return ["%02x" % i for i in t]


def PrettifyRawMessage(m):
    if m is None: return "None"

    out = Hexify(m)
    out[0] = zwave.FIRST_TO_STRING.get(m[0], "??")
    if m[0] != zwave.SOF:
        return " ".join(out)
    out[1] = "len:" + out[1]
    if m[2] == zwave.REQUEST:
        out[2] = "REQU"
    if m[2] == zwave.RESPONSE:
        out[2] = "RESP"
    out[-1] = "chk:" + out[-1]
    func = m[3]
    if func in zwave.API_TO_STRING:
        out[3] = zwave.API_TO_STRING[func] + ":" + out[3]
    if func == zwave.API_ZW_APPLICATION_UPDATE and len(m) > 5:
        out[5] = "node:" + out[5]
        if m[2] == zwave.REQUEST:
            out[4] = zwave.UPDATE_STATE_TO_STRING[m[4]]
    elif func == zwave.API_APPLICATION_COMMAND_HANDLER and len(m) > 8:
        out[6] = "len:" + out[6]
        out[5] = "node:" + out[5]
        s = zwave.SUBCMD_TO_STRING.get(m[7] * 256 + m[8])
        if s:
            out[7] = s + ":" + out[7]
            out[8] = "X:" + out[8]
        else:
            logging.error("did not find command %s %s  [%s]", m[7], m[8], m)

    elif (func == zwave.API_ZW_ADD_NODE_TO_NETWORK or
          func == zwave.API_ZW_REMOVE_NODE_FROM_NETWORK or
          func == zwave.API_ZW_SET_LEARN_MODE) and len(m) > 7:

        if len(m) == 7:
            # sednding
            out[4] = "kind:" + out[4]
            out[5] = "cb:" + out[5]
        else:
            # receiving
            out[4] = "cb:" + out[4]
            out[5] = "status:" + out[5]
    elif (func == zwave.API_ZW_REQUEST_NODE_INFO or
          func == zwave.API_ZW_GET_NODE_PROTOCOL_INFO or
          func == zwave.API_ZW_GET_ROUTING_INFO or
          func == zwave.API_ZW_IS_FAILED_NODE_ID) and len(m) > 4:
        if m[2] == zwave.REQUEST and len(out) > 4:
            out[4] = "node:" + out[4]
    elif func == zwave.API_ZW_ADD_NODE_TO_NETWORK:
        out[4] = zwave.ADD_NODE_TO_STRING[m[4]]
    elif (func == zwave.API_ZW_SEND_DATA or
          func == zwave.API_ZW_REPLICATION_SEND_DATA) and len(m) > 7:
        if m[2] == zwave.REQUEST:
            if len(m) == 7 or len(m) == 9:
                out[4] = "cb:" + out[4]
                out[5] = "status:" + out[5]
            else:
                out[4] = "node:" + out[4]
                out[-2] = "cb:" + out[-2]
                out[-3] = "xmit:" + out[-3]
                s = zwave.SUBCMD_TO_STRING.get(m[6] * 256 + m[7])
                if s:
                    out[6] = s + ":" + out[6]
                    out[7] = "X:" + out[7]
                else:
                    logging.error(
                        "did not find command (send (repl.)data) %s %s [%s]",
                        m[6], m[7], m)

    return " ".join(out)


def RawMessageFuncId(data):
    return data[-2]


def RawMessageDstNode(data):
    if len(data) < 5:
        return -1
    if data[3] == zwave.API_ZW_SEND_DATA:
        return data[4]
    return -1

def RawMessageIsRequest(data):
    if len(data) < 5:
        return -1
    return data[2] == zwave.REQUEST


def RawMessageCommandType(data):
    if len(data) < 5:
        return -1
    return data[3]


def ExtracRawMessage(data):
    if len(data) < 5:
        return None
    if data[0] != zwave.SOF:
        return None
    length = data[1]
    # +2: includes the SOF byte and the length byte
    if len(data) < length + 2:
        return None
    return data[0:length + 2]

# ==================================================

def MakeRawMessage(func, data):
    out = [zwave.SOF, len(data) + 3, zwave.REQUEST, func] + data
    # check sum over everything except the first byte
    out.append(Checksum(out) ^ zwave.SOF)
    return bytes(out)


def MakeRawMessageWithId(func, data, cb_id=None):
    if cb_id is None:
        cb_id = CallbackId()
    out = [zwave.SOF, len(data) + 4, zwave.REQUEST, func] + data + [cb_id]
    # check sum over everything except the first byte
    out.append(Checksum(out) ^ zwave.SOF)
    return bytes(out)


def MakeRawCommandWithId(node, data, xmit, cb_id=None):
    out = [node, len(data)] + data + [xmit]
    return MakeRawMessageWithId(zwave.API_ZW_SEND_DATA, out, cb_id)


def MakeRawReplicationCommandWithId(node, data, xmit, cb_id=None):
    out = [node, len(data)] + data + [xmit]
    return MakeRawMessageWithId(zwave.API_ZW_REPLICATION_SEND_DATA, out, cb_id)


def MakeRawCommandMultiWithId(nodes, data, xmit, cb_id=None):
    out = [len(nodes)] + nodes + [len(data)] + data + [xmit]
    return MakeRawMessageWithId(zwave.API_ZW_SEND_DATA_MULTI, out, cb_id)


def MakeRawCommand(node, data, xmit):
    out = [node, len(data)] + data + [xmit]
    return MakeRawMessage(zwave.API_ZW_SEND_DATA, out)


def MakeRawReplicationSendDataWithId(node, data, xmit, cb_id=None):
    out = [node, len(data)] + data + [xmit]
    return MakeRawMessageWithId(zwave.API_ZW_REPLICATION_SEND_DATA, out, cb_id)

RAW_MESSAGE_ACK = bytes([zwave.ACK])
RAW_MESSAGE_NAK = bytes([zwave.NAK])
RAW_MESSAGE_CAN = bytes([zwave.CAN])


# ==================================================
# Message
# ==================================================


MESSAGE_STATE_CREATED = "Created"
MESSAGE_STATE_STARTED = "Started"
MESSAGE_STATE_COMPLETED = "Completed"
MESSAGE_STATE_ABORTED = "Aborted"
MESSAGE_STATE_TIMEOUT = "Timeout"
MESSAGE_STATE_NOT_READY = "NotReady"

# TODO: explain these in detail
ACTION_INVALID = 0
ACTION_DELIVERED = 1
ACTION_DONE = 3
ACTION_NONE = 4
ACTION_REPORT_NE = 5
ACTION_REPORT = 6
ACTION_MATCH_CBID_MULTI = 7
ACTION_MATCH_CBID = 8
ACTION_NO_REPORT = 9
ACTION_REPORT_EQ = 10



# maps inflight message type to the action taken when a matching response is received
_RESPONSE_ACTION = {
    zwave.API_ZW_REMOVE_FAILED_NODE_ID:  [ACTION_REPORT_NE, 0], # removal started
    zwave.API_ZW_SET_DEFAULT: [ACTION_NONE],
}

_REQUEST_ACTION = {
    zwave.API_ZW_REMOVE_FAILED_NODE_ID: [ACTION_MATCH_CBID, 7],
    zwave.API_ZW_SET_DEFAULT: [ACTION_MATCH_CBID, 6],
}

_COMMANDS_WITH_NO_ACTION = [
    zwave.API_SERIAL_API_APPL_NODE_INFORMATION,
    zwave.API_ZW_SET_PROMISCUOUS_MODE,
]


for x in _COMMANDS_WITH_NO_ACTION:
    _RESPONSE_ACTION[x] = [ACTION_NONE]
    _REQUEST_ACTION[x] = [ACTION_NONE]


_COMMANDS_WITH_RESPONSE_ACTION_REPORT = [
    zwave.API_ZW_GET_SUC_NODE_ID,
    zwave.API_ZW_GET_VERSION,
    zwave.API_ZW_MEMORY_GET_ID,
    zwave.API_ZW_GET_CONTROLLER_CAPABILITIES,
    zwave.API_SERIAL_API_GET_CAPABILITIES,
    zwave.API_ZW_GET_RANDOM,
    zwave.API_SERIAL_API_GET_INIT_DATA,
    zwave.API_SERIAL_API_SET_TIMEOUTS,
    zwave.API_ZW_GET_NODE_PROTOCOL_INFO,
    zwave.API_ZW_IS_FAILED_NODE_ID,
    zwave.API_ZW_GET_ROUTING_INFO,
    zwave.API_ZW_READ_MEMORY,
    zwave.API_SERIAL_API_SOFT_RESET,
    zwave.API_ZW_ENABLE_SUC,
    zwave.API_ZW_SET_SUC_NODE_ID,
    zwave.API_ZW_REQUEST_NODE_INFO,
]


for x in _COMMANDS_WITH_RESPONSE_ACTION_REPORT:
    _RESPONSE_ACTION[x] = [ACTION_REPORT]
    _REQUEST_ACTION[x] = [ACTION_NONE]


_COMMANDS_WITH_SIMPLE_RESPONSE_AND_REQUEST = {
    zwave.API_ZW_SEND_DATA: [7, 9],
    zwave.API_ZW_SEND_DATA_MULTI: [7],
    zwave.API_ZW_SEND_NODE_INFORMATION: [7],
    zwave.API_ZW_REPLICATION_SEND_DATA: [7],
}

for x, y in _COMMANDS_WITH_SIMPLE_RESPONSE_AND_REQUEST.items():
    _RESPONSE_ACTION[x] = [ACTION_REPORT_EQ, 1]
    _REQUEST_ACTION[x] =  [ACTION_MATCH_CBID] + y


_COMMANDS_WITH_MULTI_REQUESTS = [
    zwave.API_ZW_ADD_NODE_TO_NETWORK,
    zwave.API_ZW_REMOVE_NODE_FROM_NETWORK,
    zwave.API_ZW_CONTROLLER_CHANGE,
    zwave.API_ZW_SET_LEARN_MODE,
]

for x in _COMMANDS_WITH_MULTI_REQUESTS:
    _RESPONSE_ACTION[x] = [ACTION_NONE]
    _REQUEST_ACTION[x] = [ACTION_MATCH_CBID_MULTI]

# TODO
# RESPONSE
# zwave.API_ZW_SET_SUC_NODE_ID: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_DELETE_SUC_RETURN_ROUTE: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_REPLACE_FAILED_NODE: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_DELETE_RETURN_ROUTE: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_ASSIGN_RETURN_ROUTE: [ACTION_REPORT_NE, -1],

# zwave.API_ZW_SEND_SLAVE_NODE_INFO: [ACTION_REPORT_NE, -1],

# zwave.API_ZW_SEND_NODE_INFORMATION: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_REQUEST_NETWORK_UPDATE: [ACTION_REPORT_NE, -1],

# REQUEST
# zwave.API_ZW_SET_SUC_NODE_ID: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_DELETE_SUC_RETURN_ROUTE: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_REQUEST_NODE_INFO: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_REPLACE_FAILED_NODE: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_DELETE_RETURN_ROUTE: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_ASSIGN_RETURN_ROUTE: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_SEND_SLAVE_NODE_INFO: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_SEND_NODE_INFORMATION: [ACTION_REPORT_NE, -1],
# zwave.API_ZW_REQUEST_NETWORK_UPDATE: [ACTION_REPORT_NE, -1],


class Message:
    """Message describes and outgoing message and the actions/callbacks used to determine
    when it has been fully processed.

    """
    def __init__(self, payload, priority, callback, node,
                 timeout=1.0, action_requ=None, action_resp=None):
        self.payload = payload
        self.priority = priority
        self.node = node
        self.callback = callback
        self.timeout = timeout
        self.start = None
        self.end = None
        self.can = 0
        self.aborted = False
        self.state = MESSAGE_STATE_CREATED
        self.inflight_lock = None
        if payload is None:
            return
        func = payload[3]
        # mode = payload[4]
        if action_requ is None:
            self.action_requ = _REQUEST_ACTION[func]
        else:
            self.action_requ = action_requ
        if action_resp is None:
            self.action_resp = _RESPONSE_ACTION[func]
        else:
            self.action_resp = action_resp

    def _Timeout(self):
        if self.inflight_lock is None: return
        self.Complete(None, MESSAGE_STATE_TIMEOUT)

    def Start(self, lock):
        self.state = MESSAGE_STATE_STARTED
        self.start = time.time()
        self.inflight_lock = lock
        self.inflight_lock.acquire()
        threading.Timer(self.timeout, self._Timeout).start()

    def _CompleteNoMessage(self, state):
        if self.inflight_lock is None:
            logging.warning("message already completed: ", self.state)
            return
        if state != MESSAGE_STATE_COMPLETED:
            self.aborted = True
        self.state = state
        self.end = time.time()
        logging.warning("%s: %s", state, PrettifyRawMessage(self.payload))
        self.inflight_lock.release()
        self.inflight_lock = None
        return state

    def Complete(self, m, state):
        if self.callback: self.callback(m)
        return self._CompleteNoMessage(state)


    def _MaybeCompleteAck(self, m):
        if (self.action_requ[0] == ACTION_NONE and
            self.action_resp[0] == ACTION_NONE):
            self.Complete(m, MESSAGE_STATE_COMPLETED)
        else:
            return ""

    def _MaybeCompleteRequest(self, m):
        cbid = self.payload[-2]
        if self.action_requ[0] == ACTION_MATCH_CBID_MULTI:
            if m[4] != cbid:
                logging.error("unexpected call back id: %s",
                              PrettifyRawMessage(m))
                return "unexpected"
            assert self.callback is not None
            if not self.callback(m):
                return "continue"
            return self.CompleteNoMessage(MESSAGE_STATE_COMPLETED)
        elif self.action_requ[0] == ACTION_MATCH_CBID:
            if m[4] != cbid:
                logging.error("unexpected call back id: %s",
                              PrettifyRawMessage(m))
                return "unexpected"
            return self.Complete(m, MESSAGE_STATE_COMPLETED)

        else:
            logging.error("unexpected action: %s for %s",
                          self.action_requ[0], PrettifyRawMessage(self.payload))
            assert False

    def _MaybeCompleteResponse(self, m):
        if self.action_resp[0] == ACTION_REPORT:
            return self.Complete(m, MESSAGE_STATE_COMPLETED)
        elif self.action_resp[0] == ACTION_REPORT_EQ:
            assert len(m) == 6
            # we expect a message of the form:
            # SOF <len> RES  <func> <status> <checksum>
            if self.action_resp[1] == m[4]:
                # we got the expected status everything is dandy
                # but still need to wait for the matching req
                # Note, we currently do not record having received m
                # as we have not seen failure modes requiring it.
                logging.debug("delivered to stack")
                if self.callback: self.callback(m)
                return "continue"
            else:
                logging.warning("unexpected resp status is %d wanted %d ==== %s",
                                m[4], self.action_resp[1],
                                PrettifyRawMessage(self.payload))
                return self.Complete(m, MESSAGE_STATE_NOT_READY)
            return False

        else:
            assert False


    def MaybeComplete(self, m):
        if m[0] == zwave.ACK:
            return self._MaybeCompleteAck(m)

        if m[0] != zwave.SOF:
            assert False

        func = self.payload[3]
        if m[3] != func:
            logging.error("unexpected request/response: ")
            return "unexpected"

        if m[2] == zwave.RESPONSE:
            return self._MaybeCompleteResponse(m)
        elif m[2] == zwave.REQUEST:
            return self._MaybeCompleteRequest(m)
        else:
            assert False

    def __str__(self):
        out = [PrettifyRawMessage(self.payload), ]
        if self.start and not self.end:
            out.append(" running for %dms" %
                       int(1000.0 * (time.time() - self.start)))
        return " ".join(out)

    def __lt__(self, other):
        return self.priority < other.priority



# ==================================================
# Message Queue
# ==================================================


class MessageQueue:
    """MessageQueue is a cental abstraction which allows us to decouple
    Controller, Driver and NodeSet from each other.
    It really consists of three seperate queues:
    * _out_queue: messages to be sent to the device (usb serial)
                  typically messages going to nodes
    * _in_queue: messages: messages coming from the device
                 typically messages coming from the nodes
    * _inflight_result: an internal queue for messages pertaining
                        to the _inflight Message

    Only one outgoing Message can ever be inflight.
    And serveral messages coming back from the device might be needed
    before the Message is fully processed.
    """
    def __init__(self):
        # outgoing message
        self._out_queue = MessageQueueOut()
        # unsolicited incoming - raw messages
        # TODO: should this be its own class?
        self._in_queue = queue.Queue()

    def __str__(self):
        out = ["queue length: %d" % self._out_queue.qsize(),
               "by node: %s" % str(self._out_queue)]
        return "\n".join(out)


    def EnqueueMessage(self, m):
        self._out_queue.put(m.priority, m)

    def GetIncommingRawMessage(self):
        return self._in_queue.get()

    def PutIncommingRawMessage(self, rm):
        self._in_queue.put(rm)

    def DequeueMessage(self):
        return self._out_queue.get()

    def MaybeCancelLearningOperation(self):
        if not self._inflight:
            return
        func = self._inflight.payload[3]
        if func in [zwave.API_ZW_ADD_NODE_TO_NETWORK, zwave.API_ZW_REMOVE_NODE_FROM_NETWORK]:
            self._inflight_result.put(None)


    def WaitUntilAllPreviousMessagesHaveBeenHandled(self):
        semaphore = threading.Semaphore()
        semaphore.acquire()
        # send dummy message to clear out pipe
        mesg = Message(None, LowestPriority(), lambda _: semaphore.release(), None)
        self.EnqueueMessage(mesg)
        # wait until semaphore is released by callback
        semaphore.acquire()
