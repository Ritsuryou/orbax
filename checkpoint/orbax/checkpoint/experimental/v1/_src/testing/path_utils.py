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

"""Utilities for working with paths in tests."""

import asyncio
from orbax.checkpoint.experimental.v1._src.path import types as path_types

Path = path_types.Path
PathLike = path_types.PathLike
PathAwaitingCreation = path_types.PathAwaitingCreation


class PathAwaitingCreationWrapper(path_types.PathAwaitingCreation):
  """PathAwaitingCreation where the path is already created."""

  def __init__(self, path: Path):
    self._path = path

  def __truediv__(
      self, other: PathAwaitingCreation | PathLike
  ) -> PathAwaitingCreation:
    if isinstance(other, PathAwaitingCreation):
      other = other.path
    return PathAwaitingCreationWrapper(self._path / other)

  @property
  def path(self) -> Path:
    return self._path

  async def await_creation(self) -> Path:
    await asyncio.sleep(0)
    return self._path
