# -*- test-case-name: buildbot.broken_test.test_web -*-

import os, time, shutil
import warnings
from HTMLParser import HTMLParser
from twisted.python import components

from twisted.trial import unittest
from buildbot.broken_test.runutils import RunMixin, monkeypatch

from twisted.internet import reactor, defer, protocol
from twisted.internet.interfaces import IReactorUNIX
from twisted.web import client

from buildbot import master, interfaces, sourcestamp
from buildbot.db import dbspec, schema
from buildbot.buildrequest import BuildRequest
from buildbot.status import html, builder
from buildbot.status.web import waterfall, xmlrpc
from buildbot.changes.changes import Change
from buildbot.process import base
from buildbot.process.buildstep import BuildStep
from buildbot.broken_test.runutils import setupBuildStepStatus

monkeypatch()

class SimpleMaster(master.BuildMaster):
    """This BuildMaster variant uses a local sqlite DB, and gives more
    control over the loading of the config file."""

    def __init__(self, basedir):
        # create the db here, for lack of somewhere better to do it
        dbfile = os.path.join(basedir, "state.sqlite")
        if os.path.exists(dbfile):
            os.unlink(dbfile)
        spec = dbspec.DBSpec.from_url("sqlite:///state.sqlite", basedir=basedir)

        sm = schema.DBSchemaManager(spec, basedir)
        sm.upgrade()

        master.BuildMaster.__init__(self, basedir)
        self.readConfig = True

components.registerAdapter(master.Control, SimpleMaster, interfaces.IControl)


base_config = """
from buildbot.changes.pb import PBChangeSource
from buildbot.status import html
from buildbot.buildslave import BuildSlave
from buildbot.schedulers import basic
from buildbot.process.factory import BuildFactory
from buildbot.config import BuilderConfig

BuildmasterConfig = c = {
    'change_source': PBChangeSource(),
    'slaves': [BuildSlave('bot1name', 'bot1passwd')],
    'schedulers': [basic.Scheduler(name='name', branch=None,
                treeStableTimer=60, builderNames=['builder1'])],
    'slavePortnum': 0,
    }
c['builders'] = [
    BuilderConfig(name='builder1', slavename='bot1name', factory=BuildFactory()),
]

c['db_url'] = 'sqlite:///state.sqlite'
"""



class DistribUNIX:
    def __init__(self, unixpath):
        from twisted.web import server, resource, distrib
        root = resource.Resource()
        self.r = r = distrib.ResourceSubscription("unix", unixpath)
        root.putChild('remote', r)
        self.p = p = reactor.listenTCP(0, server.Site(root))
        self.portnum = p.getHost().port
    def shutdown(self):
        d = defer.maybeDeferred(self.p.stopListening)
        return d

class DistribTCP:
    def __init__(self, port):
        from twisted.web import server, resource, distrib
        root = resource.Resource()
        self.r = r = distrib.ResourceSubscription("localhost", port)
        root.putChild('remote', r)
        self.p = p = reactor.listenTCP(0, server.Site(root))
        self.portnum = p.getHost().port
    def shutdown(self):
        d = defer.maybeDeferred(self.p.stopListening)
        d.addCallback(self._shutdown_1)
        return d
    def _shutdown_1(self, res):
        return self.r.publisher.broker.transport.loseConnection()

class SlowReader(protocol.Protocol):
    didPause = False
    count = 0
    data = ""
    def __init__(self, req):
        self.req = req
        self.d = defer.Deferred()
    def connectionMade(self):
        self.transport.write(self.req)
    def dataReceived(self, data):
        self.data += data
        self.count += len(data)
        if not self.didPause and self.count > 10*1000:
            self.didPause = True
            self.transport.pauseProducing()
            reactor.callLater(2, self.resume)
    def resume(self):
        self.transport.resumeProducing()
    def connectionLost(self, why):
        self.d.callback(None)

class CFactory(protocol.ClientFactory):
    def __init__(self, p):
        self.p = p
    def buildProtocol(self, addr):
        self.p.factory = self
        return self.p

def stopHTTPLog():
    # grr.
    from twisted.web import http
    http._logDateTimeStop()

class BaseWeb:
    master = None

    def tearDown(self):
        stopHTTPLog()
        if self.master:
            d = self.master.stopService()
            return d

    def find_webstatus(self, master):
        for child in list(master):
            if isinstance(child, html.WebStatus):
                return child

class Ports(BaseWeb, unittest.TestCase):

    def tearDown(self):
        if self.master:
            return self.master.stopService()

    def test_webPortnum(self):
        # run a regular web server on a TCP socket
        config = base_config + "c['status'] = [html.WebStatus(http_port=0)]\n"
        os.mkdir("test_web1")
        self.master = m = SimpleMaster("test_web1")
        m.startService()
        d = m.loadConfig(config)
        def _started(ign):
            # hack to find out what randomly-assigned port it is listening on
            port = self.find_webstatus(m).getPortnum()
            return client.getPage("http://localhost:%d/waterfall" % port)
        d.addCallback(_started)
        def _check(page):
            #print page
            self.failUnless(page)
        d.addCallback(_check)
        return d
    test_webPortnum.timeout = 10

    def test_webPathname(self):
        # running a t.web.distrib server over a UNIX socket
        if not IReactorUNIX.providedBy(reactor):
            raise unittest.SkipTest("UNIX sockets not supported here")
        config = (base_config +
                  "c['status'] = [html.WebStatus(distrib_port='.web-pb')]\n")
        os.mkdir("test_web2")
        self.master = m = SimpleMaster("test_web2")
        m.startService()
        d = m.loadConfig(config)
        def _started(ign):
            self.p = p = DistribUNIX("test_web2/.web-pb")
            return client.getPage("http://localhost:%d/remote/waterfall" % p.portnum)
        d.addCallback(_started)
        def _check(page):
            self.failUnless(page)
        d.addCallback(_check)
        def _done(res):
            d1 = self.p.shutdown()
            d1.addCallback(lambda x: res)
            return d1
        d.addBoth(_done)
        return d
    test_webPathname.timeout = 10


    def test_webPathname_port(self):
        # running a t.web.distrib server over TCP
        config = (base_config +
                  "c['status'] = [html.WebStatus(distrib_port=0)]\n")
        os.mkdir("test_web3")
        self.master = m = SimpleMaster("test_web3")
        m.startService()
        d = m.loadConfig(config)
        def _started(ign):
            dport = self.find_webstatus(m).getPortnum()

            self.p = p = DistribTCP(dport)

            return client.getPage("http://localhost:%d/remote/waterfall" % p.portnum)
        d.addCallback(_started)
        def _check(page):
            self.failUnlessIn("http://buildbot.net", page)
        d.addCallback(_check)
        def _done(res):
            d1 = self.p.shutdown()
            d1.addCallback(lambda x: res)
            return d1
        d.addBoth(_done)
        return d
    test_webPathname_port.timeout = 10


class Waterfall(BaseWeb, unittest.TestCase):
    def setUp(self):
        warnings.filterwarnings("ignore", category=DeprecationWarning)

    def tearDown(self):
        BaseWeb.tearDown(self)
        warnings.resetwarnings()

    def test_waterfall(self):
        os.mkdir("test_web4")
        os.mkdir("my-maildir"); os.mkdir("my-maildir/new")
        self.robots_txt = os.path.abspath(os.path.join("test_web4",
                                                       "robots.txt"))
        self.robots_txt_contents = "User-agent: *\nDisallow: /\n"
        f = open(self.robots_txt, "w")
        f.write(self.robots_txt_contents)
        f.close()
        # this is the right way to configure the Waterfall status
        config1 = base_config + """
from buildbot.changes import mail
c['change_source'] = mail.SyncmailMaildirSource('my-maildir')
c['status'] = [html.WebStatus(http_port=0)]
"""

        self.master = m = SimpleMaster("test_web4")
        m.startService()
        d = m.loadConfig(config1)
        def _started(ign):
            self.port = port = self.find_webstatus(m).getPortnum()
            # insert an event
            m.change_svc.addChange(Change("user", ["foo.c"], "comments"))

            return client.getPage("http://localhost:%d/waterfall" % port)
        d.addCallback(_started)

        def _check1(page):
            self.failUnless(page)
            self.failUnlessIn("current activity", page)
            self.failUnlessIn("<html", page)
            TZ = time.tzname[time.localtime()[-1]]
            self.failUnlessIn("(%s)" % TZ, page)

            return client.getPage("http://localhost:%d/waterfall" % self.port)
        d.addCallback(_check1)

        def _check2(page):
            self.failUnless(page)
            self.failUnlessIn("<html", page)

            return client.getPage("http://localhost:%d/changes" % self.port)
        d.addCallback(_check2)

        def _check3(changes):
            self.failUnlessIn("Syncmail mailing list in maildir " +
                              "my-maildir", changes)

        d.addCallback(_check3)

        return d

    test_waterfall.timeout = 10

class WaterfallSteps(unittest.TestCase):

    def failUnlessSubstring(self, substr, string, msg=None):
        self.assertIn(substr, string, msg or "%r not found in %r"
                                    % (substr, string))
    assertSubstring = failUnlessSubstring

    def test_urls(self):
        s = setupBuildStepStatus("test_web.test_urls")
        s.addURL("coverage", "http://coverage.example.org/target")
        s.addURL("icon", "http://coverage.example.org/icon.png")


        class FakeService:
            import jinja2
            templates = jinja2.Environment(loader=jinja2.PackageLoader('buildbot.status.web', 'templates'),
                                       extensions=['jinja2.ext.i18n'],
                                       trim_blocks=True)

        class FakeSite:
            buildbot_service = FakeService()

        class FakeRequest:
            site = FakeSite()
            prepath = []
            postpath = []
            def childLink(self, name):
                return name

        req = FakeRequest()
        box = waterfall.IBox(s).getBox(req)
        text = box.td()['text']
        e1 = '[<a href="http://coverage.example.org/target" class="BuildStep external">coverage</a>]'
        self.failUnlessSubstring(e1, text)
        e2 = '[<a href="http://coverage.example.org/icon.png" class="BuildStep external">icon</a>]'
        self.failUnlessSubstring(e2, text)



geturl_config = """
from buildbot.status import html
from buildbot.changes import mail
from buildbot.process import factory
from buildbot.steps import dummy
from buildbot.schedulers import basic
from buildbot.changes.base import ChangeSource
from buildbot.buildslave import BuildSlave
from buildbot.config import BuilderConfig
s = factory.s

class DiscardScheduler(basic.Scheduler):
    def addChange(self, change):
        pass
class DummyChangeSource(ChangeSource):
    pass

BuildmasterConfig = c = {}
c['slaves'] = [BuildSlave('bot1', 'sekrit'), BuildSlave('bot2', 'sekrit')]
c['change_source'] = DummyChangeSource()
c['schedulers'] = [DiscardScheduler(name='discard', branch=None,
                treeStableTimer=60, builderNames=['b1'])]
c['slavePortnum'] = 0
c['status'] = [html.WebStatus(http_port=0)]

f = factory.BuildFactory([s(dummy.RemoteDummy, timeout=1)])

c['builders'] = [
    BuilderConfig(name='b1', slavenames=['bot1', 'bot2'], factory=f),
]
c['buildbotURL'] = 'http://dummy.example.org:8010/'

"""

class GetURL(RunMixin, unittest.TestCase):
    def setUp(self):
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        RunMixin.setUp(self)
        d = self.master.loadConfig(geturl_config)
        d.addCallback(lambda ign: self.connectSlave(["b1"]))
        return d

    def tearDown(self):
        stopHTTPLog()
        warnings.resetwarnings()
        return RunMixin.tearDown(self)

    def doBuild(self, buildername):
        brs = self.control.submitBuildSet([buildername],
                                          sourcestamp.SourceStamp(), "forced")
        return brs.waitUntilFinished()

    def assertNoURL(self, target):
        self.failUnlessIdentical(self.status.getURLForThing(target), None)

    def assertURLEqual(self, target, expected):
        got = self.status.getURLForThing(target)
        full_expected = "http://dummy.example.org:8010/" + expected
        self.failUnlessEqual(got, full_expected)

    def testMissingBase(self):
        noweb_config1 = geturl_config + "del c['buildbotURL']\n"
        d = self.master.loadConfig(noweb_config1)
        d.addCallback(self._testMissingBase_1)
        return d
    def _testMissingBase_1(self, res):
        s = self.status
        self.assertNoURL(s)
        builder_s = s.getBuilder("b1")
        self.assertNoURL(builder_s)

    def testBase(self):
        s = self.status
        self.assertURLEqual(s, "")
        builder_s = s.getBuilder("b1")
        self.assertURLEqual(builder_s, "builders/b1")

    def testChange(self):
        c = Change("user", ["foo.c"], "comments")
        self.master.change_svc.addChange(c)
        # TODO: something more like s.getChanges(), requires IChange and
        # an accessor in IStatus. The HTML page exists already, though
        self.assertURLEqual(c, "changes/1")

    def testBuild(self):
        # first we do some stuff so we'll have things to look at.
        d = self.doBuild("b1")
        # maybe check IBuildSetStatus here?
        d.addCallback(self._testBuild_1)
        return d

    def _testBuild_1(self, res):
        s = self.status
        builder_s = s.getBuilder("b1")
        build_s = builder_s.getLastFinishedBuild()
        self.assertURLEqual(build_s, "builders/b1/builds/0")
        # no page for builder.getEvent(-1)
        step = build_s.getSteps()[0]
        self.assertURLEqual(step, "builders/b1/builds/0/steps/remote%20dummy")
        # maybe page for build.getTestResults?
        self.assertURLEqual(step.getLogs()[0],
                            "builders/b1/builds/0/steps/remote%20dummy/logs/stdio")



class Logfile(BaseWeb, unittest.TestCase):
    def create(self):
        config = """
from buildbot.status import html
from buildbot.process.factory import BasicBuildFactory
from buildbot.buildslave import BuildSlave
from buildbot.config import BuilderConfig

f1 = BasicBuildFactory('cvsroot', 'cvsmodule')
BuildmasterConfig = c = {
    'slaves': [BuildSlave('bot1', 'passwd1')],
    'schedulers': [],
    'slavePortnum': 0,
    'status': [html.WebStatus(http_port=0)],
    }
c['builders'] = [
    BuilderConfig(name='builder1', slavename='bot1', factory=f1),
]
"""
        if os.path.exists("test_logfile"):
            shutil.rmtree("test_logfile")
        os.mkdir("test_logfile")
        self.master = m = SimpleMaster("test_logfile")
        m.startService()
        d = m.loadConfig(config)
        def _started(ign):
            # hack to find out what randomly-assigned port it is listening on
            port = self.find_webstatus(m).getPortnum()
            self.port = port
            # insert an event

            req = BuildRequest("reason", sourcestamp.SourceStamp(), 'test_builder')
            build1 = base.Build([req])
            bs = m.status.getBuilder("builder1").newBuild()
            bs.setReason("reason")
            bs.buildStarted(build1)

            step1 = BuildStep(name="setup")
            step1.setBuild(build1)
            bss = bs.addStepWithName("setup")
            step1.setStepStatus(bss)
            bss.stepStarted()

            log1 = step1.addLog("output")
            log1.addStdout(u"s\N{LATIN CAPITAL LETTER O WITH GRAVE}me stdout\n".encode("utf-8"))
            log1.finish()

            log2 = step1.addHTMLLog("error", "<html>ouch</html>")
            del log2

            log3 = step1.addLog("big")
            log3.addStdout("big log\n")
            for i in range(1000):
                log3.addStdout("a" * 500)
                log3.addStderr("b" * 500)
            log3.finish()

            log4 = step1.addCompleteLog("bigcomplete",
                                        "big2 log\n" + "a" * 1*1000*1000)
            del log4

            log5 = step1.addLog("mixed")
            log5.addHeader("header content")
            log5.addStdout("this is stdout content")
            log5.addStderr("errors go here")
            log5.addEntry(5, "non-standard content on channel 5")
            log5.addStderr(" and some trailing stderr")

            d1 = defer.maybeDeferred(step1.step_status.stepFinished,
                                     builder.SUCCESS)
            bs.buildFinished()
            return d1
        d.addCallback(_started)
        return d

    def getLogPath(self, stepname, logname):
        return ("/builders/builder1/builds/0/steps/%s/logs/%s" %
                (stepname, logname))

    def getLogURL(self, stepname, logname):
        return ("http://localhost:%d" % self.port
                + self.getLogPath(stepname, logname))

    def test_logfile1(self):
        d = self.create()
        d.addCallback(lambda ign:
                      client.getPage("http://localhost:%d/" % self.port))
        def _check(page):
            self.failUnless(page)
        d.addCallback(_check)
        return d

    def test_logfile2(self):
        d = self.create()
        d.addCallback(lambda ign:
                      client.getPage(self.getLogURL("setup", "output")))
        def _check(logbody):
            self.failUnless(logbody)
        d.addCallback(_check)
        return d

    def test_logfile3(self):
        d = self.create()
        d.addCallback(lambda ign:
                      client.getPage(self.getLogURL("setup", "output") + "/text"))
        def _check(logtext):
            # verify utf-8 encoding.
            self.failUnlessEqual(logtext, u"s\N{LATIN CAPITAL LETTER O WITH GRAVE}me stdout\n".encode("utf-8"))
        d.addCallback(_check)
        return d

    def test_logfile4(self):
        d = self.create()
        d.addCallback(lambda ign:
                      client.getPage(self.getLogURL("setup", "error")))
        def _check(logbody):
            self.failUnlessEqual(logbody, "<html>ouch</html>")
        d.addCallback(_check)
        return d

    def test_logfile5(self):
        # this is log3, which is about 1MB in size, made up of alternating
        # stdout/stderr chunks. buildbot-0.6.6, when run against
        # twisted-1.3.0, fails to resume sending chunks after the client
        # stalls for a few seconds, because of a recursive doWrite() call
        # that was fixed in twisted-2.0.0
        d = self.create()
        def _created(ign):
            p = SlowReader("GET %s HTTP/1.0\r\n\r\n"
                           % self.getLogPath("setup", "big"))
            cf = CFactory(p)
            reactor.connectTCP("localhost", self.port, cf)
            d1 = p.d
            def _check(res):
                self.failUnlessIn("big log", p.data)
                self.failUnlessIn("a"*100, p.data)
                self.failUnless(p.count > 1*1000*1000)
            d1.addCallback(_check)
            return d1
        d.addCallback(_created)
        return d

    def test_logfile6(self):
        # this is log4, which is about 1MB in size, one big chunk.
        # buildbot-0.6.6 dies as the NetstringReceiver barfs on the
        # saved logfile, because it was using one big chunk and exceeding
        # NetstringReceiver.MAX_LENGTH
        d = self.create()
        def _created(ign):
            p = SlowReader("GET %s HTTP/1.0\r\n\r\n"
                           % self.getLogPath("setup", "bigcomplete"))
            cf = CFactory(p)
            reactor.connectTCP("localhost", self.port, cf)
            d1 = p.d
            def _check(res):
                self.failUnlessIn("big2 log", p.data)
                self.failUnlessIn("a"*100, p.data)
                self.failUnless(p.count > 1*1000*1000)
            d1.addCallback(_check)
            return d1
        d.addCallback(_created)
        return d

    def test_logfile7(self):
        # this is log5, with mixed content on the tree standard channels
        # as well as on channel 5

        class SpanParser(HTMLParser):
            '''Parser subclass to gather all the log spans from the log page'''
            def __init__(self, test):
                self.spans = []
                self.test = test
                self.inSpan = False
                HTMLParser.__init__(self)

            def handle_starttag(self, tag, attrs):
                if tag == 'span':
                    self.inSpan = True
                    cls = attrs[0]
                    self.test.failUnless(cls[0] == 'class')
                    self.spans.append([cls[1],''])

            def handle_data(self, data):
                if self.inSpan:
                    self.spans[-1][1] += data

            def handle_endtag(self, tag):
                if tag == 'span':
                    self.inSpan = False

        d = self.create()
        d.addCallback(lambda ign:
                      client.getPage(self.getLogURL("setup", "mixed"), timeout=2))
        def _check(logbody):
            try:
                p = SpanParser(self)
                p.feed(logbody)
                p.close
            except Exception, e:
                print e
            self.failUnlessEqual(len(p.spans), 4)
            self.failUnlessEqual(p.spans[0][0], 'header')
            self.failUnlessEqual(p.spans[0][1], 'header content')
            self.failUnlessEqual(p.spans[1][0], 'stdout')
            self.failUnlessEqual(p.spans[1][1], 'this is stdout content')
            self.failUnlessEqual(p.spans[2][0], 'stderr')
            self.failUnlessEqual(p.spans[2][1], 'errors go here')
            self.failUnlessEqual(p.spans[3][0], 'stderr')
            self.failUnlessEqual(p.spans[3][1], ' and some trailing stderr')
        def _fail(err):
            pass
        d.addCallback(_check)
        d.addErrback(_fail)
        return d

class XMLRPC(unittest.TestCase):
    def test_init(self):
        server = xmlrpc.XMLRPCServer()
        self.assert_(server)

    def test_render(self):
        self.assertRaises(NameError,
                          lambda:
                              xmlrpc.XMLRPCServer().render(Request()))
