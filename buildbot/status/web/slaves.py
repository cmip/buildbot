
import time, urllib
from twisted.web import html
from twisted.web.util import Redirect
from twisted.web.error import NoResource

from buildbot.status.web.base import HtmlResource, abbreviate_age, \
    BuildLineMixin, path_to_slave, path_to_authfail
from buildbot import util

# /buildslaves/$slavename
class OneBuildSlaveResource(HtmlResource, BuildLineMixin):
    addSlash = False
    def __init__(self, slavename):
        HtmlResource.__init__(self)
        self.slavename = slavename

    def getTitle(self, req):
        return "Buildbot: %s" % self.slavename

    def getChild(self, path, req):
        s = self.getStatus(req)
        slave = s.getSlave(self.slavename)
        if path == "shutdown":
            if self.getAuthz(req).actionAllowed("gracefulShutdown", req, slave):
                slave.setGraceful(True)
            else:
                return Redirect(path_to_authfail(req))
        return Redirect(path_to_slave(req, slave))

    def content(self, request, ctx):        
        s = self.getStatus(request)
        slave = s.getSlave(self.slavename)
        
        my_builders = []
        for bname in s.getBuilderNames():
            b = s.getBuilder(bname)
            for bs in b.getSlaves():
                if bs.getName() == self.slavename:
                    my_builders.append(b)

        # Current builds
        current_builds = []
        for b in my_builders:
            for cb in b.getCurrentBuilds():
                if cb.getSlavename() == self.slavename:                    
                    current_builds.append(self.get_line_values(request, cb))

        try:
            max_builds = int(request.args.get('numbuilds')[0])
        except:
            max_builds = 10
           
        recent_builds = []    
        n = 0
        for rb in s.generateFinishedBuilds(builders=[b.getName() for b in my_builders]):
            if rb.getSlavename() == self.slavename:
                n += 1
                recent_builds.append(self.get_line_values(request, rb))
                if n > max_builds:
                    break
        ctx.update(dict(slave = s.getSlave(self.slavename),
                        slavename = self.slavename,  
                        current = current_builds, 
                        recent = recent_builds, 
                        shutdown_url = request.childLink("shutdown"),
                        authz = self.getAuthz(request),
                        this_url = "../../../" + path_to_slave(request, slave),
                        access_uri = slave.getAccessURI()),
                        admin = unicode(slave.getAdmin() or '', 'utf-8'),
                        host = unicode(slave.getHost() or '', 'utf-8'))
        template = request.site.buildbot_service.templates.get_template("buildslave.html")
        data = template.render(**ctx)
        return data

# /buildslaves
class BuildSlavesResource(HtmlResource):
    title = "BuildSlaves"
    addSlash = True

    def content(self, request, ctx):
        s = self.getStatus(request)

        used_by_builder = {}
        for bname in s.getBuilderNames():
            b = s.getBuilder(bname)
            for bs in b.getSlaves():
                slavename = bs.getName()
                if slavename not in used_by_builder:
                    used_by_builder[slavename] = []
                used_by_builder[slavename].append(bname)
                
        slaves = ctx['slaves'] = []
        for name in util.naturalSort(s.getSlaveNames()):
            info = {}
            slaves.append(info)
            slave = s.getSlave(name)
            slave_status = s.botmaster.slaves[name].slave_status
            info['running_builds'] = len(slave_status.getRunningBuilds())
            info['link'] = request.childLink(urllib.quote(name,''))
            info['name'] = name
            
            info['builders'] = []
            for b in used_by_builder.get(name, []):
                info['builders'].append(dict(link=request.childLink("../builders/%s" % b), name=b))
                                        
            info['version'] = slave.getVersion()
            info['connected'] = slave.isConnected()
            
            if slave.isConnected():
                info['admin'] = unicode(slave.getAdmin() or '', 'utf-8')
                last = slave.lastMessageReceived()
                if last:
                    info['last_heard_from_age'] = abbreviate_age(time.time() - last)
                    info['last_heard_from_time'] = time.strftime("%Y-%b-%d %H:%M:%S",
                                                                time.localtime(last))

        template = request.site.buildbot_service.templates.get_template("buildslaves.html")
        data = template.render(**ctx)
        return data

    def getChild(self, path, req):
        try:
            self.getStatus(req).getSlave(path)
            return OneBuildSlaveResource(path)
        except KeyError:
            return NoResource("No such slave '%s'" % html.escape(path))
