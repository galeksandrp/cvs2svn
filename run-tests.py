#!/usr/bin/env python
#
#  run_tests.py:  test suite for cvs2svn
#
#  Subversion is a tool for revision control. 
#  See http://subversion.tigris.org for more information.
#    
# ====================================================================
# Copyright (c) 2000-2003 CollabNet.  All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.  The terms
# are also available at http://subversion.tigris.org/license-1.html.
# If newer versions of this license are posted there, you may use a
# newer version instead, at your option.
#
######################################################################

# General modules
import sys
import shutil
import stat
import string
import re
import os
import time
import os.path

# This script needs to run in tools/cvs2svn/.  Make sure we're there.
if not (os.path.exists('cvs2svn.py') and os.path.exists('test-data')):
  sys.stderr.write("error: I need to be run in 'tools/cvs2svn/' "
                   "in the Subversion tree.\n")
  sys.exit(1)

# Find the Subversion test framework.
sys.path += [os.path.abspath('../../subversion/tests/clients/cmdline')]
import svntest

# Abbreviations
Skip = svntest.testcase.Skip
XFail = svntest.testcase.XFail
Item = svntest.wc.StateItem

cvs2svn = os.path.abspath('cvs2svn.py')

# We use the installed svn and svnlook binaries, instead of using
# svntest.main.run_svn() and svntest.main.run_svnlook(), because the
# behavior -- or even existence -- of local builds shouldn't affect
# the cvs2svn test suite.
svn = 'svn'
svnlook = 'svnlook'

test_data_dir = 'test-data'
tmp_dir = 'tmp'


#----------------------------------------------------------------------
# Helpers.
#----------------------------------------------------------------------


def run_program(program, *varargs):
  """Run PROGRAM with VARARGS, return stdout as a list of lines.
  If there is any stderr, print it and then exit with error."""
  out, err = svntest.main.run_command(program, 1, 0, *varargs)
  if err:
    print '\n%s said:\n' % program
    for line in err: print '   ' + line,
    print
    sys.exit(1)
  return out


def run_cvs2svn(*varargs):
  """Run cvs2svn with VARARGS, return stdout as a list of lines.
  If there is any stderr, print it and then exit with error."""
  return run_program(cvs2svn, *varargs)


def run_svn(*varargs):
  """Run svn with VARARGS; return stdout as a list of lines.
  If stderr, print stderr lines and exit with error."""
  return run_program(svn, *varargs)


def repos_to_url(path_to_svn_repos):
  """This does what you think it does."""
  return 'file://%s' % os.path.abspath(path_to_svn_repos)


class Log:
  def __init__(self, revision, author, date):
    self.revision = revision
    self.author = author
    
    # Internally, we represent the date as seconds since epoch (UTC).
    # Since standard subversion log output shows dates in localtime
    #
    #   "1993-06-18 00:46:07 -0500 (Fri, 18 Jun 1993)"
    #
    # and time.mktime() converts from localtime, it all works out very
    # happily.
    self.date = time.mktime(time.strptime(date[0:19], "%Y-%m-%d %H:%M:%S"))

    # The changed paths will be accumulated later, as log data is read.
    # Keys here are paths such as '/trunk/foo/bar', values are letter
    # codes such as 'M', 'A', and 'D'.
    self.changed_paths = { }

    # The msg will be accumulated later, as log data is read.
    self.msg = ''


def parse_log(svn_repos):
  """Return a dictionary of Logs, keyed on revision number, for SVN_REPOS."""

  def absorb_changed_paths(out, log):
    'Read changed paths from OUT into Log item LOG, until no more.'
    while 1:
      line = out.readline()
      if len(line) == 1: return
      line = line[:-1]
      log.changed_paths[line[5:]] = line[3:4]

  def absorb_message_body(out, num_lines, log):
    'Read NUM_LINES of log message body from OUT into Log item LOG.'
    i = 0
    while i < num_lines:
      line = out.readline()
      log.msg += line
      i += 1

  log_start_re = re.compile('^rev (?P<rev>[0-9]+):  '
                            '(?P<author>[^\|]+) \| '
                            '(?P<date>[^\|]+) '
                            '\| (?P<lines>[0-9]+) (line|lines)$')

  log_separator = '-' * 72

  logs = { }

  # We use popen3 directly, instead of run_svn(), because it's
  # actually easier to process the results by calling readline()
  # repeatedly than it would be to have all the lines in a list.
  ign, out, err = os.popen3('%s log -v %s'
                            % (svn, repos_to_url(svn_repos)), 'b')
  err_lines = err.readlines()
  if err_lines:
    print '\n%s said:\n' % svn
    for line in err_lines: print '   ' + line,
    print
    sys.exit(1)

  while 1:
    this_log = None
    line = out.readline()
    if not line: break
    line = line[:-1]

    if line.find(log_separator) == 0:
      line = out.readline()
      line = line[:-1]
      m = log_start_re.match(line)
      if m:
        this_log = Log(int(m.group('rev')), m.group('author'), m.group('date'))
        line = out.readline()
        if not line.find('Changed paths:') == 0:
          print 'unexpected log output (missing changed paths)'
          print "Line: '%s'" % line
          sys.exit(1)
        absorb_changed_paths(out, this_log)
        absorb_message_body(out, int(m.group('lines')), this_log)
        logs[this_log.revision] = this_log
      elif len(line) == 0:
        break   # We've reached the end of the log output.
      else:
        print 'unexpected log output (missing revision line)'
        print "Line: '%s'" % line
        sys.exit(1)
    else:
      print 'unexpected log output (missing log separator)'
      print "Line: '%s'" % line
      sys.exit(1)
        
  return logs


def erase(path):
  """Unconditionally remove PATH and its subtree, if any.  PATH may be
  non-existent, a file or symlink, or a directory."""
  if os.path.isdir(path):
    shutil.rmtree(path)
  elif os.path.exists(path):
    os.remove(path)


# List of already converted names; see the NAME argument to ensure_conversion.
#
# Keys are names, values are tuples: (svn_repos, svn_wc, log_dictionary).
# The log_dictionary comes from parse_log(svn_repos).
already_converted = { }

def ensure_conversion(name):
  """Convert CVS repository NAME to Subversion, but only if it has not
  been converted before by this invocation of this script.  If it has
  been converted before, do nothing.

  NAME is just one word.  For example, 'main' would mean to convert
  './test-data/main-cvsrepos', and after the conversion, the resulting
  Subversion repository would be in './tmp/main-svnrepos', and a
  checked out head working copy in './tmp/main-wc'.

  Return the Subversion repository path and wc path. """

  cvsrepos = os.path.abspath(os.path.join(test_data_dir, '%s-cvsrepos' % name))

  if not already_converted.has_key(name):

    svnrepos = '%s-svnrepos' % name   # relative to ./tmp/, not to ./
    wc       = '%s-wc' % name         # relative to ./tmp/, not to ./

    if not os.path.isdir(tmp_dir):
      os.mkdir(tmp_dir)

    saved_wd = os.getcwd()
    try:
      os.chdir(tmp_dir)
      
      # Clean up from any previous invocations of this script.
      erase(svnrepos)
      erase(wc)
      
      run_cvs2svn('--create', '-s', svnrepos, cvsrepos)
      run_svn('co', repos_to_url(svnrepos), wc)
      log_dict = parse_log(svnrepos)
    finally:
      os.chdir(saved_wd)

    # This name is done for the rest of this session.
    already_converted[name] = (os.path.join('tmp', svnrepos),
                               os.path.join('tmp', wc),
                               log_dict)

  return already_converted[name]


#----------------------------------------------------------------------
# Tests.
#----------------------------------------------------------------------


def show_usage():
  "cvs2svn with no arguments shows usage"
  out = run_cvs2svn()
  if out[0].find('USAGE') < 0:
    print 'Basic cvs2svn invocation failed.'
    raise svntest.Failure


def attr_exec():
  "detection of the executable flag"
  repos, wc, logs = ensure_conversion('main')
  st = os.stat(os.path.join(wc, 'trunk', 'single-files', 'attr-exec'))
  if not st[0] & stat.S_IXUSR:
    raise svntest.Failure


def space_fname():
  "conversion of filename with a space"
  repos, wc, logs = ensure_conversion('main')
  if not os.path.exists(os.path.join(wc, 'trunk',
                                     'single-files', 'space fname')):
    raise svntest.Failure


def two_quick():
  "two commits in quick succession"
  repos, wc, logs = ensure_conversion('main')
  out = run_svn('log', os.path.join(wc, 'trunk', 'single-files', 'twoquick'))
  num_revisions = 0
  for line in out:
    if line.find("rev ") == 0:
      num_revisions = num_revisions + 1
  if num_revisions != 2:
    raise svntest.Failure


def prune_with_care():
  "prune, but not too eagerly"
  # Robert Pluim encountered this lovely one while converting the
  # directory src/gnu/usr.bin/cvs/contrib/pcl-cvs/ in FreeBSD's CVS
  # repository (see issue #1302).  Step 4 is the doozy:
  #
  #   revision 1:  adds trunk/, adds trunk/cookie
  #   revision 2:  adds trunk/NEWS
  #   revision 3:  deletes trunk/cookie
  #   revision 4:  deletes trunk/  [re-deleting trunk/cookie pruned trunk!]
  #   revision 5:  does nothing
  #   
  # After fixing cvs2svn, the sequence (correctly) looks like this:
  #
  #   revision 1:  adds trunk/, adds trunk/cookie
  #   revision 2:  adds trunk/NEWS
  #   revision 3:  deletes trunk/cookie
  #   revision 4:  does nothing    [because trunk/cookie already deleted]
  #   revision 5:  deletes trunk/NEWS
  # 
  # The difference is in 4 and 5.  It's not correct to prune trunk/,
  # because NEWS is still in there, so revision 4 does nothing.  But
  # when we delete NEWS in 5, that should bubble up and prune trunk/
  # instead.
  #
  # ### Note that empty revisions like 4 are probably going to become
  # ### at least optional, if not banished entirely from cvs2svn's
  # ### output.  Hmmm, or they may stick around, with an extra
  # ### revision property explaining what happened.  Need to think
  # ### about that.
  #
  # In the test below, the file 'trunk/prune-with-care/first' is
  # cookie, and 'trunk/prune-with-care/second' is NEWS.

  repos, wc, logs = ensure_conversion('main')

  # Confirm that revision 3 removes '/trunk/prune-with-care/first',
  # and that revision 5 removes '/trunk/prune-with-care'.

  if not (logs[3].changed_paths.has_key('/trunk/prune-with-care/first')
          and logs[3].changed_paths['/trunk/prune-with-care/first'] == 'D'):
    print "Revision 3 failed to remove ''/trunk/prune-with-care/first'."
    raise svntest.Failure

  if not (logs[5].changed_paths.has_key('/trunk/prune-with-care')
          and logs[5].changed_paths['/trunk/prune-with-care'] == 'D'):
    print "Revision 5 failed to remove '/trunk/prune-with-care'."
    raise svntest.Failure


#----------------------------------------------------------------------

########################################################################
# Run the tests

# list all tests here, starting with None:
test_list = [ None,
              show_usage,
              attr_exec,
              space_fname,
              two_quick,
              prune_with_care,
             ]

if __name__ == '__main__':
  svntest.main.run_tests(test_list)
  # NOTREACHED


### End of file.
