# Copyright 2024 The Orbax Authors.
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

from absl.testing import absltest
from orbax.checkpoint._src.futures import synchronization


HandlerAwaitableSignalOperationIdGenerator = (
    synchronization.HandlerAwaitableSignalOperationIdGenerator
)


class HandlerAwaitableSignalOperationIdGeneratorTest(absltest.TestCase):

  def test_is_operation_id_initialized(self):
    HandlerAwaitableSignalOperationIdGenerator._operation_id = 0

    self.assertFalse(HandlerAwaitableSignalOperationIdGenerator.is_intialized())

  def test_get_operation_id(self):
    HandlerAwaitableSignalOperationIdGenerator.next_operation_id()
    operation_id_1 = (
        HandlerAwaitableSignalOperationIdGenerator.get_current_operation_id()
    )

    HandlerAwaitableSignalOperationIdGenerator.next_operation_id()
    operation_id_2 = (
        HandlerAwaitableSignalOperationIdGenerator.get_current_operation_id()
    )

    self.assertTrue(HandlerAwaitableSignalOperationIdGenerator.is_intialized())
    self.assertEqual(operation_id_1, "1")
    self.assertEqual(operation_id_2, "2")


if __name__ == "__main__":
  absltest.main()
