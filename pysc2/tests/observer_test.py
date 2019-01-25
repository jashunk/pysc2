#!/usr/bin/python
# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Test that observer mode works in various configurations."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from absl import logging
import os

from absl.testing import absltest
from future.builtins import range  # pylint: disable=redefined-builtin

from pysc2 import maps
from pysc2 import run_configs
from pysc2.lib import point
from pysc2.lib import portspicker
from pysc2.lib import run_parallel
from pysc2.tests import utils

from s2clientprotocol import common_pb2 as sc_common
from s2clientprotocol import sc2api_pb2 as sc_pb


def print_stage(stage):
  logging.info((" %s " % stage).center(80, "-"))


class TestObserver(utils.TestCase):

  def test_observe_bots(self):
    run_config = run_configs.get()
    map_inst = maps.get("Simple64")

    with run_config.start(want_rgb=False) as controller:
      create = sc_pb.RequestCreateGame(local_map=sc_pb.LocalMap(
          map_path=map_inst.path, map_data=map_inst.data(run_config)))
      create.player_setup.add(
          type=sc_pb.Computer, race=sc_common.Random, difficulty=sc_pb.VeryEasy)
      create.player_setup.add(
          type=sc_pb.Computer, race=sc_common.Random, difficulty=sc_pb.VeryHard)
      create.player_setup.add(type=sc_pb.Observer)
      controller.create_game(create)

      join = sc_pb.RequestJoinGame(
          options=sc_pb.InterfaceOptions(),  # cheap observations
          observed_player_id=0)
      controller.join_game(join)

      outcome = False
      for _ in range(60 * 60):  # 60 minutes should be plenty.
        controller.step(16)
        obs = controller.observe()
        if obs.player_result:
          print("Outcome after %s steps (%0.1f game minutes):" % (
              obs.observation.game_loop, obs.observation.game_loop / (16 * 60)))
          for r in obs.player_result:
            print("Player %s: %s" % (r.player_id, sc_pb.Result.Name(r.result)))
          outcome = True
          break

      self.assertTrue(outcome)

  def test_observe_players(self):
    players = 2  # Can be 1.
    run_config = run_configs.get()
    parallel = run_parallel.RunParallel()
    map_inst = maps.get("Simple64")

    screen_size_px = point.Point(64, 64)
    minimap_size_px = point.Point(32, 32)
    interface = sc_pb.InterfaceOptions(raw=True, score=True)
    screen_size_px.assign_to(interface.feature_layer.resolution)
    minimap_size_px.assign_to(interface.feature_layer.minimap_resolution)

    # Reserve a whole bunch of ports for the weird multiplayer implementation.
    ports = portspicker.pick_unused_ports((players + 2) * 2)
    logging.info("Valid Ports: %s", ports)

    # Actually launch the game processes.
    print_stage("start")
    sc2_procs = [run_config.start(extra_ports=ports, want_rgb=False)
                 for _ in range(players + 1)]
    controllers = [p.controller for p in sc2_procs]

    try:
      # Save the maps so they can access it.
      map_path = os.path.basename(map_inst.path)
      print_stage("save_map")
      parallel.run((c.save_map, map_path, map_inst.data(run_config))
                   for c in controllers)

      # Create the create request.
      create = sc_pb.RequestCreateGame(
          local_map=sc_pb.LocalMap(map_path=map_path))
      create.player_setup.add(type=sc_pb.Participant)
      if players == 1:
        create.player_setup.add(type=sc_pb.Computer, race=sc_common.Random,
                                difficulty=sc_pb.VeryEasy)
      else:
        create.player_setup.add(type=sc_pb.Participant)
      create.player_setup.add(type=sc_pb.Observer)

      # Create the join request.
      joins = []
      portIdx = 2
      for i in range(players + 1):
        join = sc_pb.RequestJoinGame(options=interface)
        if i < players:
          join.race = sc_common.Random
        else:
          join.observed_player_id = 0
        join.host_ip = sc2_procs[0].host
        join.shared_port = 0  # unused
        join.server_ports.game_port = ports[0]
        join.server_ports.base_port = ports[1]
        join.client_ports.add(game_port=ports[portIdx], base_port=ports[portIdx + 1])
        portIdx = portIdx + 2
        joins.append(join)

      # Create and Join
      print_stage("create")
      controllers[0].create_game(create)
      print_stage("join")
      parallel.run((c.join_game, join) for c, join in zip(controllers, joins))

      print_stage("run")
      for game_loop in range(1, 10):  # steps per episode
        # Step the game
        parallel.run((c.step, 16) for c in controllers)

        # Observe
        obs = parallel.run(c.observe for c in controllers)
        for p_id, o in enumerate(obs):
          self.assertEqual(o.observation.game_loop, game_loop * 16)
          if p_id == players:  # ie the observer
            self.assertEqual(o.observation.player_common.player_id, 0)
          else:
            self.assertEqual(o.observation.player_common.player_id, p_id + 1)

        # Act
        actions = [sc_pb.Action() for _ in range(players)]
        for action in actions:
          pt = (point.Point.unit_rand() * minimap_size_px).floor()
          pt.assign_to(action.action_feature_layer.camera_move.center_minimap)
        parallel.run((c.act, a) for c, a in zip(controllers[:players], actions))

      # Done this game.
      print_stage("leave")
      parallel.run(c.leave for c in controllers)
    finally:
      print_stage("quit")
      # Done, shut down. Don't depend on parallel since it might be broken.
      for c in controllers:
        c.quit()
      for p in sc2_procs:
        p.close()
      portspicker.return_ports(ports)


if __name__ == "__main__":
  absltest.main()
