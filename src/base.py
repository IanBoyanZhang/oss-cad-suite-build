import os
import sys
import click
import shutil
import importlib.util
import urllib
import tempfile
import asyncio
import hashlib
import platform
from collections import OrderedDict
from libvcs.shortcuts import create_repo
from libvcs.util import run

sources = dict()
targets = dict()
architectures = [ 'linux-x64', 'darwin-x64', 'linux-arm', 'linux-arm64', 'windows-x64' ]
native_only_architectures  = [ 'darwin-x64', 'windows-x64' ]
SOURCES_ROOT = "_sources"
BUILDS_ROOT  = "_builds"
OUTPUTS_ROOT = "_outputs"
SCRIPTS_ROOT = "scripts"
PATCHES_ROOT = "patches"
RULES_ROOT   = "rules"
current_rule_group = ""

def log_warning(msg):
	click.secho("==> WARNING : ", fg="yellow", nl=False, bold=True)
	click.secho(msg, fg="white", bold=True)

def log_error(msg):
	click.secho("\n==> ERROR : ", fg="red", nl=False, bold=True)
	click.secho(msg, fg="white", bold=True)
	sys.exit(-1)

def log_info(msg):
	click.secho("==> ", fg="green", nl=False, bold=True)
	click.secho(msg, fg="white", bold=True)

def log_info_triple(msg1, msg2, msg3 = " ..."):
	click.secho("==> ", fg="green", nl=False, bold=True)
	click.secho(msg1, fg="white", nl=False, bold=True)
	click.secho(msg2, fg="green", nl=False, bold=True)
	click.secho(msg3, fg="white", bold=True)

def log_step(msg):
	click.secho("  -> ", fg="blue", nl=False, bold=True)
	click.secho(msg, fg="white", bold=True)

def log_step_triple(msg1, msg2, msg3 = " ..."):
	click.secho("  -> ", fg="blue", nl=False, bold=True)
	click.secho(msg1, nl=False, fg="white", bold=True)
	click.secho(msg2, nl=False, fg="green", bold=True)
	click.secho(msg3, fg="white", bold=True)

class SourceLocation:
	def __init__(self, name, vcs, location, revision):
		self.name = name
		self.location = location
		self.vcs = vcs
		self.revision = revision
		self.hash = None
		sources[name] = self

class Target:
	def __init__(self, name, sources = [], dependencies = [], resources = [], patches = [], arch = [], license_url = None, license_file = None, package = False):
		self.name = name
		self.sources = sources
		self.dependencies = dependencies
		self.resources = resources
		self.patches = patches
		self.license_url = license_url
		self.license_file = license_file
		self.hash = None
		self.built = False
		self.package = package
		global current_rule_group
		self.group = current_rule_group
		self.arch = arch
		if name in targets:
			log_step_triple("Overriding ", name)
		else:
			log_step("Loading {} ...".format(name))
		targets[name] = self

def getBuildOS():
	system = platform.system().lower()
	if system.startswith('mingw'):
		system = 'windows'
	return system

def getArchitecture():
	system = getBuildOS()
	machine = platform.machine().lower()
	if machine == 'x86_64':
		machine = 'x64'
	elif machine == 'aarch64':
		machine = 'arm64'
	if system == 'windows':
		machine = 'x64' if platform.architecture()[0] == '64bit' else 'x86'
	return '{}-{}'.format(system, machine)

def loadRules(group):
	global current_rule_group
	current_rule_group = group
	rules_dir = os.path.abspath(os.path.join(group, RULES_ROOT))
	log_info_triple("Loading ", group, " building rules ...")
	if not os.path.exists(rules_dir):
		log_error("Path for rule group {} does not exist.".format(group))
	for cmd_name in sorted(os.listdir(rules_dir)):
		if cmd_name.startswith("__init__") or cmd_name.startswith("base.py"):
			continue
		if cmd_name.endswith(".py"):
			try:
				spec = importlib.util.spec_from_file_location(cmd_name[:-3], os.path.abspath(os.path.join(group, RULES_ROOT, cmd_name)))
				foo = importlib.util.module_from_spec(spec)
				spec.loader.exec_module(foo)
			except Exception as e:
				log_error(str(e))

def validateRules():
	log_info("Validate building rules ...")
	usedSources = []
	for t in targets.values():
		for s in t.sources:
			usedSources.append(s)
			if s not in sources.keys():
				log_error("Unknown source {} in {} target.".format(s,t.name))
		for d in t.dependencies:
			if d not in targets.keys():
				log_error("Unknown dependancy {} in {} target.".format(d,t.name))
			if d == t.name:
				log_error("Target {} dependent on itself.".format(t.name))
		for d in t.resources:
			if d not in targets.keys():
				log_error("Unknown resources {} in {} target.".format(d,t.name))
			if d == t.name:
				log_error("Target {} use resource of itself.".format(t.name))
		for p in t.patches:
			if not os.path.exists(os.path.join(t.group, PATCHES_ROOT, p)):
				log_error("Target {} does not have corresponding patch.".format(p))

	for s in sources.keys():
		if s not in usedSources:
			log_warning("Source {} not used in any target.".format(s))

def dependencyResolver(target, resolved, unresolved, arch, display):
	node = targets[target]
	needed = True
	if node.arch and arch not in node.arch:
		needed = False
		if display:
			log_warning("Target {} not built for architecture {}.".format(node.name, arch))
	if needed:
		unresolved.append(node.name)
		for dep in node.dependencies:
			if dep not in resolved:
				if dep in unresolved:
					log_error("Circular reference detected: {} -> {}.".format(node.name, dep))
				dependencyResolver(dep, resolved, unresolved, arch, display)
		resolved.append(node.name)
		unresolved.remove(node.name)

def createBuildOrder(target, arch, display):
	resolved = []
	dependencyResolver(target, resolved, [], arch, display)
	if targets[target].package:
		while True:
			found = False
			for t in resolved:
				for r in targets[t].resources:
					if r not in resolved:
						found = True
						resolved.insert(0, r)
			if not found:
				break
	return resolved

def createNeededSourceList(target, arch):
	src = []
	for t in createBuildOrder(target, arch, False):
		for s in targets[t].sources:
			if s not in src:
				src.append(s)
	return src

def pullCode(target, arch, no_update):
	log_info("Downloading sources ...")
	for src in createNeededSourceList(target, arch):
		s = sources[src]
		repo_dir = os.path.abspath(os.path.join(SOURCES_ROOT, s.name))
		repo = create_repo(url=s.location, vcs=s.vcs, repo_dir=repo_dir)
		if not os.path.isdir(repo_dir):
			is_cloning = True
		else:
			remote_url = repo.get_remote()
			is_cloning = False
			if remote_url is None:
				log_warning("Destination dir '{}' does not contain repository data. Deleting...".format(s.name))
				is_cloning = True
				shutil.rmtree(repo_dir)
			elif remote_url!=s.location:
				log_warning("Current source location {} does not match {}. Deleting...".format(remote_url,s.location))
				is_cloning = True
				shutil.rmtree(repo_dir)

		if is_cloning:
			log_step_triple("[{}] Cloning ".format(s.name), s.location)
			try:
				repo.obtain()
			except Exception as ex:
				log_error("Error while cloning repository {}.")
		else:
			if not no_update:
				log_step_triple("[{}] Updating ".format(s.name), s.location)
				try:
					repo.update_repo()
				except Exception as ex:
					log_error("Error while updating repository {}.".format(ex))
		if is_cloning or (not no_update):
			log_step_triple("[{}] Checkout ".format(s.name), s.revision)
			repo.checkout(s.revision)
		
		s.hash =  repo.get_revision()

		log_step_triple("[{}] Current revision ".format(s.name), s.hash)

def removeError(func, path, _):
	log_error("Error while deleting {}.".format(path))

def validateTarget(target):
	if target not in targets:
		log_error("Target {} does not exist.".format(target))

def validateArch(arch):
	if arch not in architectures:
		log_error("Architecture {} does not exist.".format(arch))

def cleanBuild(arch, full):
	if not full:
		validateArch(arch)
		log_info_triple("Cleaning for ", arch, " architecture ...")

		if (os.path.exists(os.path.join(BUILDS_ROOT, arch))):
			shutil.rmtree(os.path.join(BUILDS_ROOT, arch), onerror=removeError)
		if (os.path.exists(os.path.join(OUTPUTS_ROOT, arch))):
			shutil.rmtree(os.path.join(OUTPUTS_ROOT, arch), onerror=removeError)
	else:
		log_info("Cleaning for all architectures ...")

		if (os.path.exists(BUILDS_ROOT)):
			shutil.rmtree(BUILDS_ROOT, onerror=removeError)
		if (os.path.exists(OUTPUTS_ROOT)):
			shutil.rmtree(OUTPUTS_ROOT, onerror=removeError)
		log_info("Cleaning sources ...")
		if (os.path.exists(SOURCES_ROOT)):
			shutil.rmtree(SOURCES_ROOT, onerror=removeError)

async def run_process(command, cwd, env):
	# based on https://stackoverflow.com/questions/45664626/use-pythons-pty-to-create-a-live-console
	process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=env,
			stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, bufsize=0)
	# Schedule reading from stdout and stderr as asynchronous tasks.
	stdout_f = asyncio.ensure_future(process.stdout.readline())
	stderr_f = asyncio.ensure_future(process.stderr.readline())
	while process.returncode is None:
		# Wait for a line in either stdout or stderr.
		await asyncio.wait((stdout_f, stderr_f), return_when=asyncio.FIRST_COMPLETED)
		if stdout_f.done():
			line = stdout_f.result()
			if line:
				click.secho(line.decode().rstrip())
				stdout_f = asyncio.ensure_future(process.stdout.readline())
		if stderr_f.done():
			line = stderr_f.result()
			if line:
				click.secho(line.decode().rstrip(), fg="yellow")
				stderr_f = asyncio.ensure_future(process.stderr.readline())
	return process.returncode

def run_live(command, cwd=None, env=None):
	return asyncio.get_event_loop().run_until_complete(run_process(command, cwd, env))

def calculateHash(target, prefix):
	data = []
	for s in sorted(target.sources):
		data.append(sources[s].hash)
	for d in sorted(target.dependencies):
		if targets[d].hash:
			data.append(targets[d].hash)
	for p in sorted(target.patches):
		data.append(hashlib.sha256(open(os.path.join(target.group, PATCHES_ROOT, p), 'rb').read()).hexdigest())
	data.append(hashlib.sha256(open(os.path.join(target.group, SCRIPTS_ROOT, target.name + ".sh"), 'r').read().encode()).hexdigest())
	data.append(prefix)
	return hashlib.sha256('\n'.join(data).encode()).hexdigest()

def executeBuild(target, arch, prefix, build_dir, output_dir, native, nproc, local):
	cwd = os.getcwd()

	env = OrderedDict()
	env['BUILD_OS'] = getBuildOS()
	env['WORK_DIR'] = cwd
	env['BUILD_DIR'] = os.path.abspath(build_dir)
	env['OUTPUT_DIR'] = os.path.abspath(output_dir)
	env['SRC_DIR'] = os.path.abspath(SOURCES_ROOT)
	env['PATCHES_DIR'] = os.path.abspath(os.path.join(target.group, PATCHES_ROOT))
	env['ARCH'] = arch
	env['ARCH_BASE'] = arch.split('-')[0]
	env['NPROC'] = str(nproc)
	env['SHARED_EXT'] = '.so'
	if (native):
		env['STRIP'] = 'strip'
		if (getBuildOS()=='darwin'):
			env['PATH'] =  '/usr/local/opt/gnu-sed/libexec/gnubin:'
			env['PATH'] += '/usr/local/opt/coreutils/libexec/gnubin:'
			env['PATH'] += '/usr/local/opt/qt/bin:'
			env['PATH'] += '/usr/local/opt/bison/bin:'
			env['PATH'] += '/usr/local/opt/flex/bin:'
			env['PATH'] += '/usr/local/opt/openjdk/bin:'
			env['PATH'] += os.environ['PATH']
			env['SHARED_EXT'] = '.dylib'
		elif (getBuildOS()=='windows'):
			env['CMAKE_GENERATOR'] = 'MSYS Makefiles'
			env['EXE'] = '.exe'
			env['SHARED_EXT'] = '.dll'
		else:
			env['PATH'] = os.environ['PATH']
	if os.uname()[0].startswith('MSYS_NT') or os.uname()[0].startswith('MINGW'):
		env.update(os.environ)
	env['LC_ALL'] = 'C'
	env['INSTALL_PREFIX'] = prefix
	if (local):
		env['IS_LOCAL'] = 'True'
		env['CROSS_NAME'] = os.popen('gcc -dumpmachine').read().strip()

	scriptfile = tempfile.NamedTemporaryFile()
	scriptfile.write("set -e -x\n".encode())
	scriptfile.write(open(os.path.join(target.group, SCRIPTS_ROOT, target.name + ".sh"), 'r').read().encode())
	scriptfile.flush()

	log_step("Compiling ...")
	if native:
		code = run_live(['bash', scriptfile.name], cwd=build_dir, env=env)
	else:
		params = ['docker', 
			'run', '--rm',
			'--user', '{}:{}'.format(os.getuid(), os.getgid()),
			'-v', '/tmp:/tmp',
			'-v', '{}:/work'.format(cwd),
			'-w', os.path.join('/work', os.path.relpath(build_dir, os.getcwd())),
		]
		for i, j in env.items():
			if i.endswith('_DIR'):
				params += ['-e', '{}={}'.format(i, os.path.join('/work', os.path.relpath(j, os.getcwd())))]
			else:
				params += ['-e', '{}={}'.format(i, j)]
		params += [
			'yosyshq/cross-'+ arch + ':1.0',
			'bash', scriptfile.name
		]
		code = run_live(params, cwd=build_dir)
	return code

def buildCode(target, arch, nproc, no_clean, force, prefix, local, deploy, sudo):
	if deploy and not local:
		log_error("Deployment only possible for local builds.")
	if arch != getArchitecture() and arch in native_only_architectures:
		log_error("Build for {} architecture can only be built natively.".format(arch))
	native = False
	if arch == getArchitecture() and arch in native_only_architectures:
		native = True
	if arch != getArchitecture() and local:
		log_error("Local build for {} architecture can only be built natively.".format(arch))
	if arch == getArchitecture() and local:
		native = True

	log_info_triple("Building ", target, " for {} architecture ...".format(arch))

	build_order = createBuildOrder(target, arch, True)
	pos = 0
	for t in build_order:
		pos += 1
		target = targets[t]
		target.hash = calculateHash(target, prefix)
		output_dir = os.path.join(OUTPUTS_ROOT, arch if not local else "local", target.name)

		forceBuild = force
		for dep in sorted(target.dependencies):
			forceBuild = forceBuild or targets[dep].built
		hash_file = os.path.join(output_dir, '.hash')
		if (not forceBuild and os.path.exists(hash_file)):
			if target.hash == open(hash_file, 'r').read():				
				log_info_triple("Step [{:2d}/{:2d}] skipping ".format(pos,len(build_order)), target.name)
				continue

		log_info_triple("Step [{:2d}/{:2d}] building ".format(pos,len(build_order)), target.name)

		log_step("Remove old output dir ...")
		if os.path.exists(output_dir):
			shutil.rmtree(output_dir, onerror=removeError)
		log_step("Creating output dir ...")
		os.makedirs(output_dir)

		build_dir = os.path.join(BUILDS_ROOT, arch if not local else "local", target.name)
		if no_clean and os.path.exists(build_dir):
			log_step("Skipping clean of build dir ...")
		else:
			if not target.package:
				log_step("Remove old build dir ...")
				if os.path.exists(build_dir):
					shutil.rmtree(build_dir, onerror=removeError)
				log_step("Creating build dir ...")
				os.makedirs(build_dir)
				for s in target.sources:
					src_dir = os.path.join(SOURCES_ROOT, s)
					log_step_triple("Copy '", s, "' source to build dir ...")
					run(['rsync','-a', src_dir, build_dir])

			deps = target.dependencies
			if t == target.name and target.package:
				deps = build_order
				deps.pop()

			for d in deps:
				dep = targets[d]
				needed = True
				if dep.arch and arch not in dep.arch:
					needed = False
				if needed:
					dep_dir = os.path.join(OUTPUTS_ROOT, arch if not local else "local", d)
					if not target.package:
						log_step_triple("Copy '", d, "' output to build dir ...")
						run(['rsync','-a', dep_dir, build_dir])
					else:
						log_step_triple("Copy '", d, "' output to package dir ...")
						run(['rsync','-a', dep_dir+"/", output_dir])


		code = executeBuild(target, arch, prefix, build_dir if not target.package else output_dir, output_dir, native, nproc, local)
		if code!=0:
			log_error("Script returned error code {}.".format(code))

		log_step("Marking build finished ...")
		with open(hash_file, 'w') as f:
			f.write(target.hash)
		target.built = True

		if not no_clean and not target.package:
			log_step("Remove build dir ...")
			if os.path.exists(build_dir):
				shutil.rmtree(build_dir, onerror=removeError)

	if deploy:
		log_step("Deploy {} to {} ...".format(target.name, prefix))
		out_dir = os.path.join(OUTPUTS_ROOT, "local", target.name, prefix)
		cmd = ['rsync', '-a', out_dir+"/", prefix+"/"]
		if sudo:
			cmd.insert(0, 'sudo')
		run(cmd)