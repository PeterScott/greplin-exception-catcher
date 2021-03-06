# Copyright 2011 The greplin-exception-catcher Authors.
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

"""AppEngine server for collecting exceptions."""


# pylint: disable=E0611
from google.appengine.api import memcache, taskqueue, users
# pylint: disable=E0611
from google.appengine.ext import db, webapp
# pylint: disable=E0611
from google.appengine.ext.webapp import template
# pylint: disable=E0611
from google.appengine.ext.webapp.util import run_wsgi_app

import backtrace
import collections
from datetime import datetime, timedelta
import hashlib
import itertools
try:
  from django.utils import simplejson as json
except ImportError:
  import json
import os


####### Parse the configuration. #######

def loadConfig():
  """Loads application configuration."""
  f = open('config.json')
  try:
    return json.loads(f.read())
  finally:
    f.close()


CONFIG = loadConfig()

NAME = CONFIG['name']

SECRET_KEY = CONFIG['secretKey']

REQUIRE_AUTH = CONFIG.get('requireAuth', True)



####### Utility methods. #######

def getTemplatePath(name):
  """Gets a path to the named template."""
  return os.path.join(os.path.dirname(__file__), 'templates', name)


def generateHash(exceptionType, backtraceText):
  """Generates a hash for the given exception type and backtrace."""
  hasher = hashlib.md5()
  hasher.update(exceptionType.encode('utf-8'))
  hasher.update(backtrace.normalizeBacktrace(backtraceText.encode('utf-8')))
  return hasher.hexdigest()


####### Data model. #######


class Level(object):
  """Enumeration of error levels."""

  DEBUG = 0

  INFO = 10

  WARNING = 20

  ERROR = 30



class Queue(db.Model):
  """Model for a task in the queue."""

  payload = db.TextProperty()



class Project(db.Model):
  """Model for a project that contains errors."""



class LoggedErrorV2(db.Model):
  """Model for a logged error."""

  backtrace = db.TextProperty()

  type = db.StringProperty()

  hash = db.StringProperty()

  active = db.BooleanProperty()

  count = db.IntegerProperty()

  level = db.IntegerProperty(default = Level.ERROR)

  firstOccurrence = db.DateTimeProperty()

  lastOccurrence = db.DateTimeProperty()

  lastMessage = db.StringProperty(multiline=True)

  environments = db.StringListProperty()

  servers = db.StringListProperty()



class LoggedErrorInstanceV2(db.Model):
  """Model for each occurrence of an error."""

  environment = db.StringProperty()

  date = db.DateTimeProperty()

  message = db.TextProperty()

  server = db.StringProperty()

  logMessage = db.TextProperty()

  context = db.TextProperty()

  affectedUser = db.IntegerProperty()



INSTANCE_FILTERS = ('environment', 'server', 'affectedUser')

INTEGER_FILTERS = ('affectedUser',)


def getFilters(request):
  """Gets the filters applied to the given request."""
  filters = {}
  for key, value in request.params.items():
    if key in INSTANCE_FILTERS or key == 'project':
      filters[key] = value
  return filters


def filterData(dataSet, key, value):
  """Filters a data set."""
  if key in INTEGER_FILTERS:
    return dataSet.filter(key + ' =', int(value))
  else:
    return dataSet.filter(key + ' =', value)


def getErrors(filters, limit, offset):
  """Gets a list of errors, filtered by the given filters."""
  errors = LoggedErrorV2.all().filter('active =', True)
  instanceFilters = {}
  for key, value in filters.items():
    if key == 'project':
      errors = errors.ancestor(getProject(value))
    elif key in INSTANCE_FILTERS:
      instanceFilters[key] = value
  errors = errors.order('-lastOccurrence')

  if instanceFilters:
    return list(itertools.islice(filterErrors(errors, instanceFilters), offset, offset + limit))
  else:
    return errors.fetch(limit, offset)


def filterErrors(errors, instanceFilters):
  """Filters a set of errors by the given instance filters."""
  instanceMap = collections.defaultdict(list)
  for instance in getInstances(instanceFilters):
    instanceMap[instance.parent_key()].append(instance)

  for error in errors:
    errorDict = {}
    for name, prop in LoggedErrorV2.properties().items():
      errorDict[name] = prop.get_value_for_datastore(error)
    errorDict['parent_key'] = error.parent_key()

    instances = instanceMap[error.key()]
    if instances:
      errorDict['key'] = error.key()
      errorDict['count'] = len(instances)
      errorDict['lastOccurrence'] = sorted(instances, key = lambda x: x.date, reverse = True)[0].date

      environments = set()
      servers = set()
      for instance in instances:
        environments.add(instance.environment)
        servers.add(instance.server)

      errorDict['environments'] = list(environments)
      errorDict['servers'] = list(servers)

      yield errorDict


def getInstances(filters, parent = None):
  """Gets a list of instances of the given parent error, filtered by the given filters."""

  query = LoggedErrorInstanceV2.all()
  if parent:
    query = query.ancestor(parent)

  if filters:
    for key, value in filters.items():
      if key in INSTANCE_FILTERS:
        query = filterData(query, key, value)

  return query.order('-date')


def getProject(name):
  """Gets the project with the given name."""
  serialized = memcache.get('project:%s' % name)
  if serialized:
    return db.model_from_protobuf(serialized)
  else:
    return Project.get_or_insert(name)


def getAggregatedError(project, environment, server, backtraceText, errorHash, message, timestamp):
  """Gets (and updates) the error matching the given report, or None if no matching error is found."""
  error = None

  project = getProject(project)

  key = '%s|%s' % (project, errorHash)
  serialized = memcache.get(key)
  if serialized:
    error = db.model_from_protobuf(serialized)
  else:
    q = LoggedErrorV2.all().ancestor(project).filter('hash =', errorHash).filter('active =', True)

    for possibility in q:
      if backtrace.normalizeBacktrace(possibility.backtrace) == backtrace.normalizeBacktrace(backtraceText):
        error = possibility
        break

  if error:
    error.count += 1
    error.firstOccurrence = min(error.firstOccurrence, timestamp)
    if timestamp > error.lastOccurrence:
      error.lastOccurrence = timestamp
      error.backtrace = backtraceText
      error.lastMessage = message[:300]

    if environment not in error.environments:
      error.environments.append(environment)
    if server not in error.servers:
      error.servers.append(server)
    error.put()
    memcache.set(key, db.model_to_protobuf(error))
    return error


def putException(exception):
  """Put an exception in the data store."""
  backtraceText = exception['backtrace']
  environment = exception['environment']
  message = exception['message'] or ''
  project = exception['project']
  server = exception['serverName']
  timestamp = datetime.fromtimestamp(exception['timestamp'])
  exceptionType = exception['type']
  logMessage = exception.get('logMessage')
  context = exception.get('context')

  errorHash = generateHash(exceptionType, backtraceText)

  error = getAggregatedError(project, environment, server, backtraceText, errorHash, message, timestamp)
  if not error:
    error = LoggedErrorV2(parent = getProject(project))
    error.backtrace = backtraceText
    exceptionType = exceptionType.replace('\n', ' ')
    if len(exceptionType) > 500:
      exceptionType = exceptionType[:500]
    error.type = exceptionType.replace('\n', ' ')
    error.hash = errorHash
    error.active = True
    error.count = 1
    error.firstOccurrence = timestamp
    error.lastOccurrence = timestamp
    error.lastMessage = message[:300]
    error.environments = [str(environment)]
    error.servers = [server]
    error.put()

  instance = LoggedErrorInstanceV2(parent = error)
  instance.environment = environment
  instance.date = timestamp
  instance.message = message
  instance.server = server
  instance.logMessage = logMessage
  if context:
    instance.context = json.dumps(context)
    if 'userId' in context:
      instance.affectedUser = int(context['userId'])

  instance.put()


####### Pages #######

class AuthPage(webapp.RequestHandler):
  """Base class for pages that require authentication."""

  def __getUser(self):
    """Gets a user."""
    return users.get_current_user()


  def get(self, *args):
    """Handles a get, ensuring the user is authenticated."""
    user = self.__getUser()
    if user or not REQUIRE_AUTH:
      self.doAuthenticatedGet(user, *args)
    else:
      self.redirect(users.create_login_url(self.request.uri))


  def doAuthenticatedGet(self, _, *__):
    """Performs a get with an authenticated user."""
    self.error(500)


  def post(self, *args):
    """Handles a post, ensuring the user is authenticated."""
    user = self.__getUser()
    if user or not REQUIRE_AUTH:
      self.doAuthenticatedPost(user, *args)
    else:
      self.redirect(users.create_login_url(self.request.uri))


  def doAuthenticatedPost(self, _, *__):
    """Performs a post with an authenticated user."""
    self.error(500)



class ReportPage(webapp.RequestHandler):
  """Page handler for reporting a new exception."""

  def post(self):
    """Handles a new error report via POST."""
    key = self.request.get('key')

    if key != SECRET_KEY:
      self.error(403)
      return

    # Add the task to the default queue.
    task = Queue(payload = self.request.body)
    task.put()
    taskqueue.add(url='/reportWorker', params={'key': task.key()})



class StatPage(webapp.RequestHandler):
  """Page handler for collecting error instance stats."""

  def get(self):
    """Handles a new error report via POST."""
    key = self.request.get('key')

    if key != SECRET_KEY:
      self.error(403)
      return

    counts = []
    project = self.request.get('project')
    if project:
      project = getProject(project)
      if not project:
        self.response.out.write(' '.join(['0' for _ in counts]))
    for minutes in self.request.get('minutes').split():
      query = LoggedErrorInstanceV2.all()
      if project:
        query = query.ancestor(project)
      counts.append(query.filter('date >=', datetime.now() - timedelta(minutes = int(minutes))).count())

    self.response.out.write(' '.join((str(count) for count in counts)))



class ReportWorker(webapp.RequestHandler):
  """Worker handler for reporting a new exception."""

  def post(self):
    """Handles a new error report via POST."""
    task = Queue.get(self.request.get('key'))
    if not task:
      return

    exception = json.loads(task.payload)
    putException(exception)
    task.delete()



class ListPage(AuthPage):
  """Page displaying a list of exceptions."""

  def doAuthenticatedGet(self, user):
    self.response.headers['Content-Type'] = 'text/html'

    filters = getFilters(self.request)

    page = int(self.request.get('page', 0))
    errors = getErrors(filters, limit = 51, offset = page * 50)

    hasMore = len(errors) == 51
    errors = errors[:50]

    context = {
      'title': NAME,
      'extraScripts': ['list'],
      'user': user,
      'filters': filters.items(),
      'errors': errors,
      'hasMore': hasMore,
      'nextPage': page + 1
    }
    self.response.out.write(template.render(getTemplatePath('list.html'), context))



class ViewPage(AuthPage):
  """Page displaying a single exception."""

  def doAuthenticatedGet(self, user, *args):
    key, = args
    self.response.headers['Content-Type'] = 'text/html'
    error = LoggedErrorV2.get(key)
    filters = getFilters(self.request)
    context = {
      'title': '%s - %s' % (error.lastMessage, NAME),
      'extraScripts': ['view'],
      'user': user,
      'error': error,
      'filters': filters.items(),
      'instances': getInstances(filters, parent=error)[:100]
    }
    self.response.out.write(template.render(getTemplatePath('view.html'), context))



class ResolvePage(AuthPage):
  """Page that resolves an exception."""

  def doAuthenticatedGet(self, _, *args):
    key, = args
    self.response.headers['Content-Type'] = 'text/plain'
    error = LoggedErrorV2.get(key)
    error.active = False
    error.put()

    key = '%s|%s' % (error.parent_key().name(), error.hash)
    memcache.delete(key)

    self.response.out.write('ok')



class ClearDatabasePage(AuthPage):
  """Page for clearing the database."""

  def doAuthenticatedGet(self, _):
    if users.is_current_user_admin():
      for error in LoggedErrorV2.all():
        error.delete()
      for instance in LoggedErrorInstanceV2.all():
        instance.delete()
      self.response.out.write('Done')
    else:
      self.redirect(users.create_login_url(self.request.uri))



####### Application. #######

def main():
  """Runs the server."""
  application = webapp.WSGIApplication(
      [
        ('/', ListPage),

        ('/clear', ClearDatabasePage),

        ('/report', ReportPage),
        ('/reportWorker', ReportWorker),

        ('/view/(.*)', ViewPage),
        ('/resolve/(.*)', ResolvePage),

        ('/stats', StatPage),
      ],
      debug=True)

  run_wsgi_app(application)


if __name__ == "__main__":
  main()
