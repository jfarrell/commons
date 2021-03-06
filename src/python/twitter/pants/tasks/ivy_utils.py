# ==================================================================================================
# Copyright 2012 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

from __future__ import print_function

import os
import xml
import pkgutil
import re
import threading
import errno

from collections import namedtuple, defaultdict
from contextlib import contextmanager

from twitter.common.collections import OrderedSet, maybe_list
from twitter.common.dirutil import safe_mkdir, safe_open

from twitter.pants.base.build_environment import get_buildroot
from twitter.pants.base.generator import Generator, TemplateData
from twitter.pants.base.revision import Revision
from twitter.pants.base.target import Target
from twitter.pants.ivy import Bootstrapper, Ivy
from twitter.pants.java import util

from . import TaskError


IvyModuleRef = namedtuple('IvyModuleRef', ['org', 'name', 'rev'])
IvyArtifact = namedtuple('IvyArtifact', ['path', 'classifier'])
IvyModule = namedtuple('IvyModule', ['ref', 'artifacts', 'callers'])


class IvyInfo(object):
  def __init__(self):
    self.modules_by_ref = {}  # Map from ref to referenced module.
    # Map from ref of caller to refs of modules required by that caller.
    self.deps_by_caller = defaultdict(OrderedSet)

  def add_module(self, module):
    self.modules_by_ref[module.ref] = module
    for caller in module.callers:
      self.deps_by_caller[caller].add(module.ref)


class IvyUtils(object):
  """Useful methods related to interaction with ivy."""
  def __init__(self, config, options, log):
    self._log = log
    self._config = config
    self._options = options

    # TODO(pl): This is super awful, but options doesn't have a nice way to get out
    # attributes that might not be there, and even then the attribute value might be
    # None, which we still want to override
    # Benjy thinks we should probably hoist these options to the global set of options,
    # rather than just keeping them within IvyResolve.setup_parser
    self._mutable_pattern = (getattr(options, 'ivy_mutable_pattern', None) or
                             config.get('ivy-resolve', 'mutable_pattern', default=None))

    self._transitive = config.getbool('ivy-resolve', 'transitive', default=True)
    self._args = config.getlist('ivy-resolve', 'args', default=[])
    self._jvm_options = config.getlist('ivy-resolve', 'jvm_args', default=[])
    # Disable cache in File.getCanonicalPath(), makes Ivy work with -symlink option properly on ng.
    self._jvm_options.append('-Dsun.io.useCanonCaches=false')
    self._work_dir = config.get('ivy-resolve', 'workdir')
    self._template_path = os.path.join('templates', 'ivy_resolve', 'ivy.mustache')

    if self._mutable_pattern:
      try:
        self._mutable_pattern = re.compile(self._mutable_pattern)
      except re.error as e:
        raise TaskError('Invalid mutable pattern specified: %s %s' % (self._mutable_pattern, e))

    def parse_override(override):
      match = re.match(r'^([^#]+)#([^=]+)=([^\s]+)$', override)
      if not match:
        raise TaskError('Invalid dependency override: %s' % override)

      org, name, rev_or_url = match.groups()

      def fmt_message(message, template):
        return message % dict(
            overridden='%s#%s;%s' % (template.org, template.module, template.version),
            rev=rev_or_url,
            url=rev_or_url)

      def replace_rev(template):
        self._log.info(fmt_message('Overrode %(overridden)s with rev %(rev)s', template))
        return template.extend(version=rev_or_url, url=None, force=True)

      def replace_url(template):
        self._log.info(fmt_message('Overrode %(overridden)s with snapshot at %(url)s', template))
        return template.extend(version='SNAPSHOT', url=rev_or_url, force=True)

      replace = replace_url if re.match(r'^\w+://.+', rev_or_url) else replace_rev
      return (org, name), replace
    self._overrides = {}
    # TODO(pl): See above comment wrt options
    if hasattr(options, 'ivy_resolve_overrides') and options.ivy_resolve_overrides:
      self._overrides.update(parse_override(o) for o in options.ivy_resolve_overrides)

  @staticmethod
  @contextmanager
  def cachepath(path):
    if not os.path.exists(path):
      yield ()
    else:
      with safe_open(path, 'r') as cp:
        yield (path.strip() for path in cp.read().split(os.pathsep) if path.strip())

  @staticmethod
  def symlink_cachepath(ivy_home, inpath, symlink_dir, outpath):
    """Symlinks all paths listed in inpath that are under ivy_home into symlink_dir.

    Preserves all other paths. Writes the resulting paths to outpath.
    Returns a map of path -> symlink to that path.
    """
    safe_mkdir(symlink_dir)
    with safe_open(inpath, 'r') as infile:
      paths = filter(None, infile.read().strip().split(os.pathsep))
    new_paths = []
    for path in paths:
      if not path.startswith(ivy_home):
        new_paths.append(path)
        continue
      symlink = os.path.join(symlink_dir, os.path.relpath(path, ivy_home))
      try:
        os.makedirs(os.path.dirname(symlink))
      except OSError as e:
        if e.errno != errno.EEXIST:
          raise
      # Note: The try blocks cannot be combined. It may be that the dir exists but the link doesn't.
      try:
        os.symlink(path, symlink)
      except OSError as e:
        # We don't delete and recreate the symlink, as this may break concurrently executing code.
        if e.errno != errno.EEXIST:
          raise
      new_paths.append(symlink)
    with safe_open(outpath, 'w') as outfile:
      outfile.write(':'.join(new_paths))
    symlink_map = dict(zip(paths, new_paths))
    return symlink_map

  def identify(self, targets):
    targets = list(targets)
    if len(targets) == 1 and hasattr(targets[0], 'provides') and targets[0].provides:
      return targets[0].provides.org, targets[0].provides.name
    else:
      return 'internal', Target.maybe_readable_identify(targets)

  def xml_report_path(self, targets, conf):
    """The path to the xml report ivy creates after a retrieve."""
    org, name = self.identify(targets)
    cachedir = Bootstrapper.instance().ivy_cache_dir
    return os.path.join(cachedir, '%s-%s-%s.xml' % (org, name, conf))

  def parse_xml_report(self, targets, conf):
    """Returns the IvyInfo representing the info in the xml report, or None if no report exists."""
    path = self.xml_report_path(targets, conf)
    if not os.path.exists(path):
      return None

    ret = IvyInfo()
    etree = xml.etree.ElementTree.parse(self.xml_report_path(targets, conf))
    doc = etree.getroot()
    for module in doc.findall('dependencies/module'):
      org = module.get('organisation')
      name = module.get('name')
      for revision in module.findall('revision'):
        rev = revision.get('name')
        artifacts = []
        for artifact in revision.findall('artifacts/artifact'):
          artifacts.append(IvyArtifact(path=artifact.get('location'),
                                       classifier=artifact.get('extra-classifier')))
        callers = []
        for caller in revision.findall('caller'):
          callers.append(IvyModuleRef(caller.get('organisation'),
                                      caller.get('name'),
                                      caller.get('callerrev')))
        ret.add_module(IvyModule(IvyModuleRef(org, name, rev), artifacts, callers))
    return ret

  def _extract_classpathdeps(self, targets):
    """Subclasses can override to filter out a set of targets that should be resolved for classpath
    dependencies.
    """
    def is_classpath(target):
      return (target.is_jar or
              target.is_internal and any(jar for jar in target.jar_dependencies if jar.rev))

    classpath_deps = OrderedSet()
    for target in targets:
      classpath_deps.update(t for t in target.resolve() if t.is_concrete and is_classpath(t))
    return classpath_deps

  def _generate_ivy(self, targets, jars, excludes, ivyxml, confs):
    org, name = self.identify(targets)
    template_data = TemplateData(
        org=org,
        module=name,
        version='latest.integration',
        publications=None,
        configurations=confs,
        dependencies=[self._generate_jar_template(jar, confs) for jar in jars],
        excludes=[self._generate_exclude_template(exclude) for exclude in excludes])

    safe_mkdir(os.path.dirname(ivyxml))
    with open(ivyxml, 'w') as output:
      generator = Generator(pkgutil.get_data(__name__, self._template_path),
                            root_dir=get_buildroot(),
                            lib=template_data)
      generator.write(output)

  def _calculate_classpath(self, targets):

    def is_jardependant(target):
      return target.is_jar or target.is_jvm

    jars = {}
    excludes = set()

    # Support the ivy force concept when we sanely can for internal dep conflicts.
    # TODO(John Sirois): Consider supporting / implementing the configured ivy revision picking
    # strategy generally.
    def add_jar(jar):
      coordinate = (jar.org, jar.name)
      existing = jars.get(coordinate)
      jars[coordinate] = jar if not existing else (
        self._resolve_conflict(existing=existing, proposed=jar)
      )

    def collect_jars(target):
      if target.is_jar:
        add_jar(target)
      elif target.jar_dependencies:
        for jar in target.jar_dependencies:
          if jar.rev:
            add_jar(jar)

      # Lift jvm target-level excludes up to the global excludes set
      if target.is_jvm and target.excludes:
        excludes.update(target.excludes)

    for target in targets:
      target.walk(collect_jars, is_jardependant)

    return jars.values(), excludes

  def _resolve_conflict(self, existing, proposed):
    if proposed == existing:
      return existing
    elif existing.force and proposed.force:
      raise TaskError('Cannot force %s#%s to both rev %s and %s' % (
        proposed.org, proposed.name, existing.rev, proposed.rev
      ))
    elif existing.force:
      self._log.debug('Ignoring rev %s for %s#%s already forced to %s' % (
        proposed.rev, proposed.org, proposed.name, existing.rev
      ))
      return existing
    elif proposed.force:
      self._log.debug('Forcing %s#%s from %s to %s' % (
        proposed.org, proposed.name, existing.rev, proposed.rev
      ))
      return proposed
    else:
      try:
        if Revision.lenient(proposed.rev) > Revision.lenient(existing.rev):
          self._log.debug('Upgrading %s#%s from rev %s  to %s' % (
            proposed.org, proposed.name, existing.rev, proposed.rev,
          ))
          return proposed
        else:
          return existing
      except Revision.BadRevision as e:
        raise TaskError('Failed to parse jar revision', e)

  def _is_mutable(self, jar):
    if jar.mutable is not None:
      return jar.mutable
    if self._mutable_pattern:
      return self._mutable_pattern.match(jar.rev)
    return False

  def _generate_jar_template(self, jar, confs):
    template = TemplateData(
        org=jar.org,
        module=jar.name,
        version=jar.rev,
        mutable=self._is_mutable(jar),
        force=jar.force,
        excludes=[self._generate_exclude_template(exclude) for exclude in jar.excludes],
        transitive=jar.transitive,
        artifacts=jar.artifacts,
        configurations=[conf for conf in jar.configurations if conf in confs])
    override = self._overrides.get((jar.org, jar.name))
    return override(template) if override else template

  def _generate_exclude_template(self, exclude):
    return TemplateData(org=exclude.org, name=exclude.name)

  def is_classpath_artifact(self, path):
    """Subclasses can override to determine whether a given artifact represents a classpath
    artifact."""
    return path.endswith('.jar') or path.endswith('.war')

  def is_mappable_artifact(self, org, name, path):
    """Subclasses can override to determine whether a given artifact represents a mappable
    artifact."""
    return self.is_classpath_artifact(path)

  def mapto_dir(self):
    """Subclasses can override to establish an isolated jar mapping directory."""
    return os.path.join(self._work_dir, 'mapped-jars')

  def mapjars(self, genmap, target, executor, workunit_factory=None):
    """
    Parameters:
      genmap: the jar_dependencies ProductMapping entry for the required products.
      target: the target whose jar dependencies are being retrieved.
    """
    mapdir = os.path.join(self.mapto_dir(), target.id)
    safe_mkdir(mapdir, clean=True)
    ivyargs = [
      '-retrieve', '%s/[organisation]/[artifact]/[conf]/'
                   '[organisation]-[artifact]-[revision](-[classifier]).[ext]' % mapdir,
      '-symlink',
    ]
    self.exec_ivy(mapdir,
                  [target],
                  ivyargs,
                  confs=target.configurations,
                  ivy=Bootstrapper.default_ivy(executor),
                  workunit_factory=workunit_factory,
                  workunit_name='map-jars')

    for org in os.listdir(mapdir):
      orgdir = os.path.join(mapdir, org)
      if os.path.isdir(orgdir):
        for name in os.listdir(orgdir):
          artifactdir = os.path.join(orgdir, name)
          if os.path.isdir(artifactdir):
            for conf in os.listdir(artifactdir):
              confdir = os.path.join(artifactdir, conf)
              for f in os.listdir(confdir):
                if self.is_mappable_artifact(org, name, f):
                  # TODO(John Sirois): kill the org and (org, name) exclude mappings in favor of a
                  # conf whitelist
                  genmap.add(org, confdir).append(f)
                  genmap.add((org, name), confdir).append(f)

                  genmap.add(target, confdir).append(f)
                  genmap.add((target, conf), confdir).append(f)
                  genmap.add((org, name, conf), confdir).append(f)

  ivy_lock = threading.RLock()

  def exec_ivy(self,
               target_workdir,
               targets,
               args,
               confs=None,
               ivy=None,
               workunit_name='ivy',
               workunit_factory=None,
               symlink_ivyxml=False):

    ivy = ivy or Bootstrapper.default_ivy()
    if not isinstance(ivy, Ivy):
      raise ValueError('The ivy argument supplied must be an Ivy instance, given %s of type %s'
                       % (ivy, type(ivy)))

    ivyxml = os.path.join(target_workdir, 'ivy.xml')
    jars, excludes = self._calculate_classpath(targets)

    ivy_args = ['-ivy', ivyxml]

    confs_to_resolve = confs or ['default']
    ivy_args.append('-confs')
    ivy_args.extend(confs_to_resolve)

    ivy_args.extend(args)
    if not self._transitive:
      ivy_args.append('-notransitive')
    ivy_args.extend(self._args)

    def safe_link(src, dest):
      if os.path.exists(dest):
        os.unlink(dest)
      os.symlink(src, dest)

    with IvyUtils.ivy_lock:
      self._generate_ivy(targets, jars, excludes, ivyxml, confs_to_resolve)
      runner = ivy.runner(jvm_options=self._jvm_options, args=ivy_args)
      try:
        result = util.execute_runner(runner,
                                     workunit_factory=workunit_factory,
                                     workunit_name=workunit_name)

        # Symlink to the current ivy.xml file (useful for IDEs that read it).
        if symlink_ivyxml:
          ivyxml_symlink = os.path.join(self._work_dir, 'ivy.xml')
          safe_link(ivyxml, ivyxml_symlink)

        if result != 0:
          raise TaskError('Ivy returned %d' % result)
      except runner.executor.Error as e:
        raise TaskError(e)
