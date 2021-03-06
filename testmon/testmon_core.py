import zlib

try:
    import configparser
except ImportError:
    import ConfigParser as configparser
import json
import os
from collections import defaultdict
import sys
import textwrap
import random

import coverage
from testmon.process_code import checksum_coverage
from testmon.process_code import Module
import hashlib

if sys.version_info > (3,):
    buffer = memoryview
    encode = lambda x: bytes(x, 'utf_8')
else:
    encode = lambda x: x


def _get_python_lib_paths():
    res = [sys.prefix]
    for attr in ['exec_prefix', 'real_prefix', 'base_prefix']:
        if getattr(sys, attr, sys.prefix) not in res:
            res.append(getattr(sys, attr))
    return [os.path.join(d, "*") for d in res]


def flip_dictionary(node_data):
    files = defaultdict(lambda: {})
    for nodeid, node_files in node_data.items():
        for filename, checksums in node_files.items():
            files[filename][nodeid] = checksums
    return files


def unaffected(node_data, changed_files):
    file_data = flip_dictionary(node_data)
    unaffected_nodes = dict(node_data)
    unaffected_files = set(file_data)
    for file in set(changed_files) & set(file_data):
        for nodeid, checksums in file_data[file].items():
            if set(checksums) - set(changed_files[file].checksums):
                affected = set(unaffected_nodes.pop(nodeid, []))
                unaffected_files = unaffected_files - affected
    return unaffected_nodes, unaffected_files


class Testmon(object):
    def __init__(self, project_dirs, testmon_labels=set()):
        self.project_dirs = project_dirs
        self.testmon_labels = testmon_labels
        self.setup_coverage(not ('singleprocess' in testmon_labels))

    def setup_coverage(self, subprocess):
        includes = [os.path.join(path, '*') for path in self.project_dirs]
        if subprocess:
            self.setup_subprocess(includes)

        self.cov = coverage.Coverage(include=includes,
                                     omit=_get_python_lib_paths(),
                                     data_file=getattr(self, 'sub_cov_file', None),
                                     config_file=False, )
        self.cov._warn_no_data = False

    def setup_subprocess(self, includes):
        if not os.path.exists('.tmontmp'):
            os.makedirs('.tmontmp')
        self.sub_cov_file = os.path.abspath('.tmontmp/.testmoncoverage' + str(random.randint(0, 1000000)))
        with open(self.sub_cov_file + "_rc", "w") as subprocess_rc:
            rc_content = textwrap.dedent("""\
                    [run]
                    data_file = {}
                    include = {}
                    omit = {}
                    parallel=True
                    """).format(self.sub_cov_file,
                                "\n ".join(includes),
                                "\n ".join(_get_python_lib_paths())
                                )
            subprocess_rc.write(rc_content)
        os.environ['COVERAGE_PROCESS_START'] = self.sub_cov_file + "_rc"

    def track_dependencies(self, callable_to_track, testmon_data, rootdir, nodeid):
        self.start()
        try:
            callable_to_track()
        except:
            raise
        finally:
            self.stop_and_save(testmon_data, rootdir, nodeid)

    def start(self):
        self.cov.erase()
        self.cov.start()

    def stop_and_save(self, testmon_data, rootdir, nodeid, result=[]):
        self.cov.stop()
        if hasattr(self, 'sub_cov_file'):
            self.cov.combine()

        testmon_data.set_dependencies(nodeid, testmon_data.get_nodedata(nodeid, self.cov.get_data(), rootdir), result)

    def close(self):
        if hasattr(self, 'sub_cov_file'):
            os.remove(self.sub_cov_file + "_rc")
        os.environ.pop('COVERAGE_PROCESS_START', None)


def eval_variant(run_variant, **kwargs):
    if not run_variant:
        return ''

    def md5(s):
        return hashlib.md5(s.encode()).hexdigest()

    eval_globals = {'os': os, 'sys': sys, 'hashlib': hashlib, 'md5': md5}
    eval_globals.update(kwargs)

    try:
        return str(eval(run_variant, eval_globals))
    except Exception as e:
        return repr(e)


def get_variant_inifile(inifile):
    config = configparser.ConfigParser()
    config.read(str(inifile), )
    if config.has_section('pytest') and config.has_option('pytest', 'run_variant_expression'):
        run_variant_expression = config.get('pytest', 'run_variant_expression')
    else:
        run_variant_expression = None

    return eval_variant(run_variant_expression)


def read_file_with_checksum(absfilename):
    afile = open(absfilename)
    hasher = hashlib.sha1()
    source = afile.read()
    hasher.update(encode(source))
    return source, hasher.hexdigest()


def parse_file(filename, rootdir, source_code):
    return Module(source_code=source_code, file_name=filename, rootdir=rootdir)


class SourceTree():
    def __init__(self, rootdir, mtimes, checksums):
        self.rootdir = rootdir
        self.mtimes = mtimes
        self.checksums = checksums
        self.changed_files = {}

    def get_changed_files(self):

        for filename in self.mtimes:
            try:
                absfilename = os.path.join(self.rootdir, filename)
                fs_mtime = os.path.getmtime(absfilename)
                if self.mtimes[filename] != fs_mtime:
                    self.mtimes[filename] = fs_mtime
                    code, fs_checksum = read_file_with_checksum(absfilename)
                    if self.checksums.get(filename) != fs_checksum:
                        self.checksums[filename] = fs_checksum
                        self.changed_files[filename] = parse_file(filename=filename, rootdir=self.rootdir,
                                                                  source_code=code)

            except OSError:
                pass
                # self.changed_files[filename] = DISAPPEARED_FILE

        return self.changed_files

    def get_file(self, filename):
        if filename not in self.changed_files:
            code, checksum = read_file_with_checksum(os.path.join(self.rootdir, filename))
            self.mtimes[filename] = os.path.getmtime(os.path.join(self.rootdir, filename))
            self.checksums[filename] = checksum
            self.changed_files[filename] = parse_file(filename=filename, rootdir=self.rootdir, source_code=code)
        return self.changed_files[filename]


class TestmonData(object):
    def __init__(self, rootdir, variant=None):

        self.variant = variant if variant else 'default'
        self.rootdir = rootdir
        self.init_connection()
        self.node_data = {}
        self.reports = defaultdict(lambda: [])

    def init_connection(self):
        self.datafile = os.path.join(self.rootdir, '.testmondata')
        self.connection = None
        import sqlite3

        if os.path.exists(self.datafile):
            self.newfile = False
        else:
            self.newfile = True
        self.connection = sqlite3.connect(self.datafile)
        self.connection.execute("PRAGMA recursive_triggers = TRUE ")
        if getattr(self, 'newfile', False):
            self.init_tables()

    def _fetch_attribute(self, attribute, default=None):
        cursor = self.connection.execute("SELECT data FROM metadata WHERE dataid=?",
                                         [self.variant + ':' + attribute])
        result = cursor.fetchone()
        if result:
            return json.loads(result[0])  # zlib.decompress(result[0]).decode('utf-8)'))
        else:
            return default

    def _fetch_node_data(self):
        dependencies = defaultdict(lambda: {})
        for row in self.connection.execute("""SELECT
                                                node_name,
                                                file_name,
                                                checksums
                                              FROM node_file WHERE node_variant=?""",
                                           (self.variant,)):
            dependencies[row[0]][row[1]] = json.loads(row[2])

        fail_reports = defaultdict(lambda: {})

        for row in self.connection.execute('SELECT name, result FROM node WHERE variant=? AND failed=1',
                                           (self.variant,)):
            fail_reports[row[0]] = json.loads(row[1])

        return dependencies, fail_reports

    def _write_attribute(self, attribute, data):
        dataid = self.variant + ':' + attribute
        json_data = json.dumps(data)
        compressed_data_buffer = json_data  # buffer(zlib.compress(json_data.encode('utf-8')))
        cursor = self.connection.execute("UPDATE metadata SET data=? WHERE dataid=?",
                                         [compressed_data_buffer, dataid])
        if not cursor.rowcount:
            cursor.execute("INSERT INTO metadata VALUES (?, ?)",
                           [dataid, compressed_data_buffer])

    def init_tables(self):
        self.connection.execute('CREATE TABLE metadata (dataid TEXT PRIMARY KEY, data TEXT)')
        self.connection.execute("""
          CREATE TABLE node (
              variant TEXT,
              name TEXT,
              result TEXT,
              failed BIT,
              PRIMARY KEY (variant, name))
""")
        self.connection.execute("""
          CREATE TABLE node_file (
            node_variant TEXT,
            node_name TEXT,
            file_name TEXT,
            checksums TEXT,
            FOREIGN KEY(node_variant, node_name) REFERENCES node(variant, name) ON DELETE CASCADE)
    """)

    def read_data(self):
        self.node_data, self.fail_reports = self._fetch_node_data()

    def write_data(self):
        with self.connection:
            if hasattr(self, 'source_tree'):
                self._write_attribute('mtimes', self.source_tree.mtimes)
                self._write_attribute('file_checksums', self.source_tree.checksums)

    def collect_garbage(self, removed_nodeids):
        for removed_nodeid in removed_nodeids:
            self.node_data.pop(removed_nodeid, None)
        self.connection.executemany('DELETE FROM node WHERE variant=? AND name=?',
                                    [(self.variant, removed_nodeid) for removed_nodeid in removed_nodeids])

    def repr_per_node(self, key):
        return "{}: {}\n".format(key,
                                 [(os.path.relpath(p), checksum)
                                  for (p, checksum)
                                  in self.node_data[key].items()])

    def test_should_run(self, nodeid):
        if nodeid in self.unaffected_nodeids:
            return False
        else:
            return True

    def file_data(self):
        return flip_dictionary(self.node_data)

    def get_nodedata(self, nodeid, coverage_data, rootdir):
        result = {}
        for filename in coverage_data.measured_files():
            relfilename = os.path.relpath(filename, rootdir)
            lines = coverage_data.lines(filename)
            if os.path.exists(filename):
                result[relfilename] = checksum_coverage(self.source_tree.get_file(relfilename).blocks, lines)
        if not result:  # when testmon kicks-in the test module is already imported. If the test function is skipped
            # coverage_data is empty. However, we need to write down, that we depend on the
            # file where the test is stored (so that we notice e.g. when the test is no longer skipped.)
            relfilename = os.path.relpath(os.path.join(rootdir, nodeid).split("::", 1)[0], self.rootdir)
            result[relfilename] = checksum_coverage(self.source_tree.get_file(relfilename).blocks, [1])
        return result

    def set_dependencies(self, nodeid, nodedata, result=[]):
        with self.connection as con:
            outcome = bool([True for r in result if r.get('outcome') == u'failed'])
            con.execute("INSERT OR REPLACE INTO "
                        "node "
                        "VALUES (?, ?, ?, ?)",
                        (self.variant, nodeid, json.dumps(result) if outcome else '', outcome))
            con.executemany("INSERT INTO node_file VALUES (?, ?, ?, ?)",
                            [(self.variant, nodeid, filename, json.dumps(nodedata[filename])) for filename in nodedata])

    def read_source(self):
        mtimes = self._fetch_attribute('mtimes', default={})
        checksums = self._fetch_attribute('file_checksums', default={})

        self.source_tree = SourceTree(rootdir=self.rootdir, mtimes=mtimes, checksums=checksums)
        self.compute_unaffected(self.source_tree.get_changed_files())

    def compute_unaffected(self, changed_files):
        self.unaffected_nodeids, self.unaffected_files = unaffected(self.node_data, changed_files)

        # possible data structures
        # nodeid1 -> [filename -> [block_a, block_b]]
        # filename -> [block_a -> [nodeid1, ], block_b -> [nodeid1], block_c -> [] ]
