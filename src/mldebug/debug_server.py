# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2024-2025 Advanced Micro Devices, Inc. All rights reserved.

"""
Communicate with flexmlrt to dump spill buffers or terminate the application
"""

import os
import struct
import socket
from mldebug.utils import LOGGER

FIXED_BUFFER_SIZE = 512


class DebugServer:
  """
  A class to handle the dumping of spill buffers,
  and communication with flexmlrt for buffer dump and termination requests.
  """

  def __init__(self, subgraph_name, output_dir, is_testmode, bind_addr=("127.0.0.1", 9000)) -> None:
    """
    Initialize the DebugServer instance.

    Args:
      subgraph_name (str): Name of the subgraph (used for naming dumped buffers).
      output_dir (str): Directory where buffer dumps will be stored.
      is_testmode (bool): Enables test mode, which disables socket operations for CI/testing.
      bind_addr (tuple): Address and port to bind the debug server socket.
    """
    self.bind_addr = bind_addr
    self.subgraph_name = subgraph_name
    self.output_dir = output_dir
    self.is_testmode = is_testmode
    self.server_socket = None
    self.client_socket = None
    self.start()

  def start(self):
    """
    Starts the debug server by listening for a single client connection and accepting it.

    This method will block until a client connects, unless in test mode. If a client is already connected,
    it logs a message and returns immediately.

    Returns:
      bool: True if the server started/listened successfully or is in test mode, False otherwise.
    """
    if self.is_testmode:
      LOGGER.verbose_print("Debug server started.")
      return True
    if self.client_socket:
      LOGGER.verbose_print(
        "A client is already connected to flexmlrt. "
        "Close the existing connection first if you want to accept a new one."
      )
      return True
    try:
      if not self.server_socket:
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(self.bind_addr)
        self.server_socket.listen(1)
        LOGGER.verbose_print(f"Listening on {self.bind_addr}...")

      self.client_socket, client_address = self.server_socket.accept()
      LOGGER.log(f"[INFO] Connected to FlexmlRT on {client_address}")
      return True
    except socket.error as e:
      LOGGER.verbose_print(f"Socket error during setup or connection: {e}")
      return False

  def pad_string(self, input_str):
    """
    Pads the input string and prepares it to send over the network.

    Args:
      input_str (str): String to be padded and encoded.

    Returns:
      bytes: String as a fixed-size, null-terminated byte array.
    """
    input_bytes = input_str.encode("utf-8")
    if len(input_bytes) >= FIXED_BUFFER_SIZE:
      input_bytes = input_bytes[:FIXED_BUFFER_SIZE]

    return input_bytes.ljust(FIXED_BUFFER_SIZE, b"\0")

  def send_request(self, name, offset, size, current_dir=False):
    """
    Prepares and sends dump request data (buffer name, offset, size) to the connected client,
    then waits for an acknowledgement (ACK).

    Args:
      name (str): Buffer name or identifier.
      offset (int): Offset in the buffer.
      size (int): Size of the buffer to dump, in bytes.
      current_dir (bool): If True, use current working directory as output prefix;
                          otherwise use the configured output directory.

    Returns:
      bool: True if request is sent and acknowledged successfully; False otherwise or if in test mode.
    """
    if self.is_testmode:
      LOGGER.verbose_print(f"Send data {name} at {offset} {size}")
      return True

    if not self.client_socket:
      LOGGER.verbose_print("No connection exists with flexmlrt. Request Failed.")
      return False

    if current_dir:
      file_prefix = os.getcwd()
    else:
      file_prefix = os.path.join(os.getcwd(), self.output_dir)

    filename = ""
    if self.subgraph_name:
      filename = os.path.normpath(os.path.join(file_prefix, "spillBO_" + self.subgraph_name + "_id_0_" + name + ".bin"))
    else:
      filename = os.path.normpath(os.path.join(file_prefix, name + ".bin"))
    padded_filename_bytes = self.pad_string(filename)
    packed_offset = struct.pack("<I", offset)
    packed_size = struct.pack("<I", size)

    message_payload = padded_filename_bytes + packed_offset + packed_size

    try:
      self.client_socket.sendall(message_payload)
      LOGGER.verbose_print(
        f"Sent Data for layer '{name}': offset={offset}, size={size} (Total {len(message_payload)} bytes)"
      )

      ack = self.client_socket.recv(1024)
      if ack:
        LOGGER.verbose_print(f"Response: {ack.decode('utf-8', errors='ignore')}")
        return True
      LOGGER.verbose_print("Closed flexmlrt connection without sending ACK.")
      return False
    except socket.error as e:
      LOGGER.verbose_print(f"Socket error during communication with flexmlrt: {e}")
      return False

  def send_termination_request(self):
    """
    Sends a termination request to the client.

    Instructs the connected FlexmlRT client to shutdown its connection,
    waits for an acknowledgement, and logs responses or errors.

    Returns:
      bool: True if termination command is sent and acknowledged or in test mode, False otherwise.
    """
    if self.is_testmode:
      LOGGER.verbose_print("Sent MLDebugger debug server termination request.")
      return True
    if not self.client_socket:
      LOGGER.verbose_print("No connection exists with flexmlrt. Termination request failed.")
      return False

    padded_filename_bytes = self.pad_string("TERMINATE_CONNECTION")
    packed_offset = struct.pack("<I", 0)
    packed_size = struct.pack("<I", 0)

    message_payload = padded_filename_bytes + packed_offset + packed_size

    try:
      self.client_socket.sendall(message_payload)
      LOGGER.verbose_print("Sent termination request to flexmlrt")

      ack = self.client_socket.recv(1024)
      if ack:
        LOGGER.verbose_print(f"Response: {ack.decode('utf-8', errors='ignore')}")
        return True
      LOGGER.verbose_print("Closed flexmlrt without sending acknowledgement.")
    except socket.error as e:
      LOGGER.log(f"[ERROR] Socket error during termination request: {e}")
    return False

  def close(self):
    """
    Sends a termination request and closes both the client connection
    and the underlying debug server socket (if open).

    Ensures resources are cleaned up on shutdown and logs any errors encountered.
    """
    if self.client_socket:
      try:
        self.send_termination_request()
        self.client_socket.close()
        self.client_socket = None
        LOGGER.verbose_print("Connection to flexmlrt closed.")
      except socket.error as e:
        LOGGER.verbose_print(f"Error closing the flexmlrt connection: {e}")

    if self.server_socket:
      try:
        self.server_socket.close()
        self.server_socket = None
        LOGGER.verbose_print("Closing MLDebugger debug server socket")
      except socket.error as e:
        LOGGER.verbose_print(f"Error closing MLDebugger debug server socket: {e}")
