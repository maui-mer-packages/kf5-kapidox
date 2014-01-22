# -*- coding: utf-8 -*-
#
# Copyright 2014  Aurélien Gâteau <agateau@kde.org>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
# OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
# NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
# THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
import fnmatch
import os
import re
import shutil
import tempfile

import yaml
import yapgvb

from framework import Framework


TARGET_SHAPES = [
    "polygon", # lib
    "house",   # executable
    "octagon", # module (aka plugin)
    "diamond", # static lib
    ]

DEPS_SHAPE = "ellipse"

DEPS_BLACKLIST = [
    "-l*", "-W*", # link flags
    "/*", # absolute dirs
    "m", "pthread", "util", "nsl", "resolv", # generic libs
    "*example*", "*demo*", "*test*", "*Test*", "*debug*" # helper targets
    ]


def to_temp_file(dirname, fname, content):
    path = os.path.join(dirname, os.path.basename(fname))
    if os.path.exists(path):
        raise Exception("{} already exists".format(path))
    open(path, "w").write(content)
    return path


def preprocess(fname):
    lst = []
    gfx = yapgvb.Graph().read(fname)
    txt = open(fname).read()
    targets = []

    # Replace the generated node names with their label. CMake generates a graph
    # like this:
    #
    # "node0" [ label="KF5DNSSD" shape="polygon"];
    # "node1" [ label="Qt5::Network" shape="ellipse"];
    # "node0" -> "node1"
    #
    # And we turn it into this:
    #
    # "KF5DNSSD" [ label="KF5DNSSD" shape="polygon"];
    # "Qt5::Network" [ label="Qt5::Network" shape="ellipse"];
    # "KF5DNSSD" -> "Qt5::Network"
    #
    # Using real framework names as labels makes it possible to merge multiple
    # .dot files.
    for node in gfx.nodes:
        label = node.label.replace("KF5::", "")
        if node.shape in TARGET_SHAPES:
            targets.append(label)
        txt = txt.replace('"' + node.name + '"', '"' + label + '"')

    # Sometimes cmake will generate an entry for the target alias, something
    # like this:
    #
    # "node9" [ label="KParts" shape="polygon"];
    # ...
    # "node15" [ label="KF5::KParts" shape="ellipse"];
    # ...
    #
    # After our node renaming, this ends up with a second "KParts" node
    # definition, which we need to get rid of.
    for target in targets:
        rx = r' *"' + target + '".*label="KF5::' + target + '".*shape="ellipse".*;'
        txt = re.sub(rx, '', txt)
    return txt


def _add_extra_dependencies(fw, yaml_file):
    dct = yaml.load(open(yaml_file))
    lst = dct.get("framework-dependencies")
    if lst is None:
        return
    for dep in lst:
        fw.add_extra_framework(dep)


class DotFileParser(object):
    def __init__(self, tmp_dir, with_qt):
        self._tmp_dir = tmp_dir
        self._with_qt = with_qt

    def parse(self, dot_file):
        # dot_file is of the form:
        # <dot-dir>/<tier>/<framework>/<framework>.dot
        lst = dot_file.split("/")
        tier = lst[-3]
        name = lst[-2]
        fw = Framework(tier, name)

        # Preprocess dot files so that they can be merged together. The
        # output needs to be stored in a temp file because yapgvb
        # crashes when reading from a StringIO
        tmp_file = to_temp_file(self._tmp_dir, dot_file, preprocess(dot_file))
        self._init_fw_from_dot_file(fw, tmp_file, self._with_qt)

        return fw

    def _init_fw_from_dot_file(self, fw, dot_file, with_qt):
        def target_from_node(node):
            return node.name.replace("KF5", "")

        src = yapgvb.Graph().read(dot_file)

        targets = set()
        for node in src.nodes:
            if node.shape in TARGET_SHAPES and self._want(node):
                target = target_from_node(node)
                targets.add(target)
                fw.add_target(target)

        for edge in src.edges:
            target = target_from_node(edge.tail)
            if target in targets and self._want(edge.head):
                dep_target = target_from_node(edge.head)
                fw.add_target_dependency(target, dep_target)

    def _want(self, node):
        if node.shape not in TARGET_SHAPES and node.shape != DEPS_SHAPE:
            return False
        name = node.name

        for pattern in DEPS_BLACKLIST:
            if fnmatch.fnmatchcase(node.name, pattern):
                return False
        if not self._with_qt and name.startswith("Qt"):
            return False
        return True


class FrameworkDb(object):
    def __init__(self):
        self._fw_list = []
        self._fw_for_target = {}

    def populate(self, dot_files, with_qt=False):
        """
        Init db from dot files
        """
        tmp_dir = tempfile.mkdtemp(prefix="kf5dot")
        parser = DotFileParser(tmp_dir, with_qt)
        try:
            for dot_file in dot_files:
                fw = parser.parse(dot_file)
                yaml_file = dot_file.replace(".dot", ".yaml")
                if os.path.exists(yaml_file):
                    _add_extra_dependencies(fw, yaml_file)
                self._fw_list.append(fw)
        finally:
            shutil.rmtree(tmp_dir)
        self._update_fw_for_target()

    def _update_fw_for_target(self):
        self._fw_for_target = {}
        for fw in self._fw_list:
            for target in fw.get_targets():
                self._fw_for_target[target] = fw

    def find_by_name(self, name):
        for fw in self._fw_list:
            if fw.name == name:
                return fw
        return None

    def remove_unused_frameworks(self, wanted_fw):
        def is_used_in_list(the_fw, lst):
            for fw in lst:
                if fw == the_fw:
                    continue
                for target in fw.get_all_target_dependencies():
                    if the_fw.has_target(target):
                        return True
                if the_fw.name in fw.get_extra_frameworks():
                    return True
            return False

        done = False
        old_lst = self._fw_list
        while not done:
            lst = []
            done = True
            for fw in old_lst:
                if fw == wanted_fw:
                    lst.append(fw)
                    continue
                if is_used_in_list(fw, old_lst):
                    lst.append(fw)
                else:
                    done = False
            old_lst = lst
        self._fw_list = lst

    def find_external_targets(self):
        all_targets = set([])
        fw_targets = set([])
        for fw in self._fw_list:
            fw_targets.update(fw.get_targets())
            all_targets.update(fw.get_all_target_dependencies())
        return all_targets.difference(fw_targets)

    def get_framework_for_target(self, target):
        return self._fw_for_target[target]

    def __iter__(self):
        return iter(self._fw_list)
