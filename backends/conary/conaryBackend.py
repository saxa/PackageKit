#!/usr/bin/python
#{{{ Licensed under the GNU General Public License Version 2
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright (C) 2007 Ken VanDine <ken@vandine.org>
# Copyright (C) 2008 Richard Hughes <richard@hughsie.com>
# Copyright (C) 2009-2010 Andres Vargas <zodman@foresightlinux.org>
#                         Scott Parkerson <scott.parkerson@gmail.com>
# }}}
#{{{ LIBS
import sys
import os
import re

from conary import dbstore, queryrep, versions, updatecmd
from conary import errors, conarycfg, conaryclient
from conary import trove
from conary.conaryclient import cmdline
from conary.deps import deps
from conary.lib import util
from conary.local import database

from packagekit.backend import get_package_id, split_package_id, \
    PackageKitBaseBackend
from packagekit.enums import *

from conaryCallback import UpdateCallback, GetUpdateCallback
from conaryCallback import RemoveCallback, UpdateSystemCallback
from conaryFilter import ConaryFilter
from XMLCache import XMLCache
from pkConaryLog import log
from conarypk import ConaryPk, get_arch

sys.excepthook = util.genExcepthook()
#{{{ FUNCTIONS
def ExceptionHandler(func):
    return func
    def display(error):
        log.info(error)
        return str(error).replace('\n', ' ').replace("\t",'')
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        #except Exception:
        #    raise
        except conaryclient.NoNewTrovesError:
            return
        except conaryclient.DepResolutionFailure, e:
            self.error(ERROR_DEP_RESOLUTION_FAILED, display(e), exit=True)
        except conaryclient.UpdateError, e:
            # FIXME: Need a enum for UpdateError
            self.error(ERROR_UNKNOWN, display(e), exit=True)
        except Exception, e:
            self.error(ERROR_UNKNOWN, display(e), exit=True)
    return wrapper

def _format_str(str):
    """
    Convert a multi line string to a list separated by ';'
    """
    if str:
        lines = str.split('\n')
        return ";".join(lines)
    else:
        return ""

def _format_list(lst):
    """
    Convert a multi line string to a list separated by ';'
    """
    if lst:
        return ";".join(lst)
    else:
        return ""
#}}}
class PackageKitConaryBackend(PackageKitBaseBackend):
    # Packages there require a reboot
    rebootpkgs = ("kernel", "glibc", "hal", "dbus")
    restartpkgs = ("PackageKit","gnome-packagekit")

    packages = []
    #{{{   Packages structure
    """
    packages = {
        pkg_name: {
            'trove': ( name,version,flavor)
            'metadata': pkgDict,
        }
    }

    """
    #}}}
    #{{{ Init
    def __init__(self, args):
        PackageKitBaseBackend.__init__(self, args)

        # conary configurations
        conary = ConaryPk()
        self.cfg = conary.cfg
        self.client = conary.cli
        self.conary = conary
        self.callback = UpdateCallback(self, self.cfg)
        self.client.setUpdateCallback(self.callback)
        self.xmlcache = XMLCache()

    def _freezeData(self, version, flavor):
        frzVersion = version.freeze()
        frzFlavor = flavor.freeze()
        return ','.join([frzVersion, frzFlavor])

    def _thawData(self, frzVersion, frzFlavor ):
        version = versions.ThawVersion(frzVersion)
        flavor = deps.ThawFlavor(frzFlavor)
        return version, flavor
    #}}}
    @ExceptionHandler
    def check_installed(self, troveTuple):
        log.info("============check installed =========")
        result = self.conary.query(troveTuple[0])
        if result:
            installed = INFO_INSTALLED
        else:
            installed = INFO_AVAILABLE
        return installed

    def get_package_id_new(self,pkg):

        name,version,flavor = pkg.get("trove")
        metadata = pkg.get("metadata")
        data = ""
        if metadata:
            if "shortDesc" in metadata:
                data = metadata['shortDesc'].decode("UTF")
                if data == "." or data == "":
                    data = name.replace("-",' ').capitalize()
        return get_package_id(name, str(version.trailingRevision()),
                get_arch(flavor), data)

    @ExceptionHandler
    def get_package_id(self, name, versionObj, flavor):

        version = versionObj.trailingRevision()
        pkg = self.xmlcache.resolve(name)
        #pkg["shortDesc"] = "."
        arch = get_arch(flavor)
        #data = versionObj.asString() + "#"
        data = ""
        if pkg:
            if "shortDesc" in pkg:
                data = pkg['shortDesc'].decode("UTF")
                if data == "." or data == "":
                    data = name.replace("-",' ').capitalize()

        return get_package_id(name, version, arch, data)

    @ExceptionHandler
    def get_package_from_id(self, package_id):
        """ package_id(string) =
        "dpaster;0.1-3-1;x86;Summary"
        """
        log.info("=========== get package from package_id ======================")
        name, verString, archString, data = split_package_id(package_id)
        trove = self.conary.query(name) or self.conary.repo_query(name)
        return trove

    def _search_package( self, name ):
        for pkg in self.packages:
            if pkg["trove"][0] == name:
                return pkg
        return None

    def _convert_package( self, trove , pkgDict ):
        return dict(
                trove = trove ,
                metadata = pkgDict
            )

    def _add_package(self, trove, pkgDict):
        self.packages.append( self._convert_package(trove, pkgDict) )

    def _do_search(self, filters, searchlist, where = "name"):
        """
         searchlist(str)ist as the package for search like
         filters(str) as the filter
        """
        fltlist = filters
        if where not in ("name", "details", "group", "all"):
            log.info("where %s" % where)
            self.error(ERROR_UNKNOWN, "DORK---- search where not found")

        log.debug((searchlist, where))
        log.info("||||||||||||||||||||||||||||searching  on cache... ")
        pkgList = self.xmlcache.search(searchlist, where )
        log.info("|||||||||||||||||||||||||||||1end searching on cache... ")

        if len(pkgList) > 0 :
            log.info("FOUND (%s) elements " % len(pkgList) )
            for pkgDict in pkgList:
                self._add_package( ( pkgDict["name"], None, None), pkgDict )

            self._resolve_list( fltlist  )
        else:
            log.info("NOT FOUND %s " % searchlist )
            self.message(MESSAGE_COULD_NOT_FIND_PACKAGE,"search not found")
            #self.error(ERROR_INTERNAL_ERROR, "packagenotfound")


    def _build_update_job(self, applyList, cache=True):
        '''Build an UpdateJob from applyList
        '''
        self.allow_cancel(False)
        updJob = self.client.newUpdateJob()
        suggMap = {}
        jobPath = self.xmlcache.checkCachedUpdateJob(applyList)
        if cache and jobPath:
            try:
                log.info("Using previously cached update job at %s" % (jobPath,))
                updJob.thaw(jobPath)
            except IOError, err:
                log.error("Failed to read update job at %s (error=%s)" % (jobPath, str(err)))
                updJob = None
        else:
            log.info("Creating a new update job")
            try:
                suggMap = self.client.prepareUpdateJob(updJob, applyList)
                log.info("Successfully created a new update job")
                if cache:
                    self.xmlcache.cacheUpdateJob(applyList, updJob)
            except conaryclient.NoNewTrovesError:
                return updJob, {}
            except conaryclient.DepResolutionFailure as error :
                log.info(error.getErrorMessage())
                deps =  error.cannotResolve
                dep_package = [ str(i[0][0]).split(":")[0] for i in deps ]
                log.info(dep_package)
                self.error(ERROR_DEP_RESOLUTION_FAILED,  "This package depends of:  %s" % ", ".join(set(dep_package)))

        return updJob, suggMap

    def _do_update(self, updJob, simulate=False):
        self.allow_cancel(False)
        try:
            # TODO we should really handle the restart case here
            restartDir = self.client.applyUpdateJob(updJob, test=simulate)
        except errors.InternalConaryError:
            self.error(ERROR_NO_PACKAGES_TO_UPDATE,"get-updates first and then update sytem")
        except trove.TroveIntegrityError:
            self.error(ERROR_NO_PACKAGES_TO_UPDATE,"run get-updates again")
        return updJob

    def _get_package_update(self, name, version, flavor):
        if name.startswith('-'):
            applyList = [(name, (version, flavor), (None, None), False)]
        else:
            applyList = [(name, (None, None), (version, flavor), True)]
        return self._build_update_job(applyList)

    def _do_package_update(self, name, version, flavor, simulate):
        updJob, suggMap = self._get_package_update(name, version, flavor)
        return self._do_update(updJob, simulate)

    def _resolve_list(self, filters):
        log.info("======= _resolve_list =====")

        # 1. Resolve through local db

        list_trove_all = [p.get("trove") for p in self.packages]
        list_installed = []
        list_not_installed = []

        if FILTER_NOT_INSTALLED in filters:
            list_not_installed = self.packages[:]
        else:
            db_trove_list = self.client.db.findTroves(None, list_trove_all, allowMissing=True)
            for trove in list_trove_all:
                pkg = self._search_package(trove[0])
                if trove in db_trove_list:
                    # A package may have different versions/flavors installed.
                    for t in db_trove_list[trove]:
                        list_installed.append(dict(trove=t, metadata=pkg["metadata"]))
                else:
                    list_not_installed.append(pkg)

        # Our list of troves doesn't contain information about whether trove is
        # installed, so ConaryFilter can't do proper filtering. Don't pass
        # @filters to it. Instead manually check the filters before calling
        # add_installed() and add_available().
        pkgFilter = ConaryFilter()
        pkgFilter.add_installed(list_installed)
        log.info("Packages installed .... %s " % len(list_installed))
        log.info("Packages available .... %s " % len(list_not_installed))

        # 2. Resolve through repository

        if FILTER_INSTALLED not in filters:
            list_trove_not_installed = []
            for pkg in list_not_installed:
                name,version,flavor = pkg.get("trove")
                trove = (name, version, self.conary.flavor)
                list_trove_not_installed.append(trove)

            list_available = []
            repo_trove_list = self.client.repos.findTroves(self.conary.default_label,
                    list_trove_not_installed, allowMissing=True)

            for trove in list_trove_not_installed:
                if trove in repo_trove_list:
                    # only use the first trove in the list
                    t = repo_trove_list[trove][0]
                    pkg = self._search_package(t[0])
                    pkg["trove"] = t
                    list_available.append(pkg)
            pkgFilter.add_available( list_available )

        package_list = pkgFilter.post_process()
        self._show_package_list(package_list)

    @ExceptionHandler
    def resolve(self, filters, package ):
        """
            @filters  (list)  list of filters
            @package (list ) list with packages name for resolve
        """
        self.allow_cancel(True)
        self.percentage(None)
        self.status(STATUS_INFO)
        log.info("filters: %s package:%s " % (filters, package))

        pkg_dict = self.xmlcache.resolve( package[0] )
        log.info(pkg_dict)
        if pkg_dict is None:
            return None

        log.info("doing a resolve")
        # Our list of troves doesn't contain information about whether trove is
        # installed, so ConaryFilter can't do proper filtering. Don't pass
        # @filters to it. Instead manually check the filters before calling
        # add_installed() and add_available().
        filter = ConaryFilter()

        is_found_locally = False
        if FILTER_NOT_INSTALLED not in filters:
            trove_installed = self.conary.query(pkg_dict.get("name"))
            log.info("end of conary query")
            for trv in trove_installed:
                pkg = self._convert_package(trv, pkg_dict)
                filter.add_installed([pkg])
                is_found_locally = True

        if not is_found_locally and FILTER_INSTALLED not in filters:
            trove_available = self.conary.repo_query(pkg_dict.get("name"))
            log.info("end of conary rquery")
            if trove_available:
                pkg = self._convert_package(trove_available[0], pkg_dict)
                filter.add_available([pkg])

        package_list = filter.post_process()
        log.info("package_list %s" % package_list)
        self._show_package_list(package_list)
	log.info("end resolve ...................")

    def _show_package_list(self, lst):
        """
            HOW its showed on packageKit
            @lst(list(tuple) = [ ( troveTuple, status ) ]
        """
        for (pos, ( pkg, status) ) in enumerate(lst):
            # take the basic info
           # name ,version,flavor = pkg.get("trove")
            # get the string id from packagekit
            #log.info(pkg)
            package_id = self.get_package_id_new(pkg)

            # split the list for get Determine info
            summary = package_id.split(";")
            name = summary[0]
            meta = summary[3]

            summary[3] = pkg.get("metadata").get("label")
            pkg_id = ";".join(summary)
            log.info("====== show the package (%s) %s- %s" %( pos, name, status) )
            self.package(package_id, status, meta )
        self.packages = []

    @ExceptionHandler
    def search_group(self, options, searchlist):
        '''
        Implement the {backend}-search-group functionality
        '''
        log.info("============= search_group ========")
        self.allow_cancel(True)
        self.percentage(None)
        self.status(STATUS_QUERY)
        log.info("options: %s searchlist:%s "%(options, searchlist))
        self._do_search(options, searchlist, 'group')

    @ExceptionHandler
    def search_file(self, filters, search ):

        log.info("============= search_file ========")
        self.allow_cancel(True)
        self.percentage(0)
        self.status(STATUS_QUERY)
        log.info("options: %s searchlist:%s "%(filters, search))
        self.percentage(10)


        self.percentage(20)


        self.percentage(30)
        name = self.conary.search_path( search )
        self.percentage(50)
        log.info(name)
        if name:
            log.info("resolving")
            if ":" in name:
                name = name.split(":")[0]
            self.resolve( filters, [name])

    @ExceptionHandler
    def search_name(self, options, searchlist):
        '''
        Implement the {backend}-search-name functionality
        '''
        log.info("============= search_name ========")
        self.allow_cancel(True)
        self.percentage(None)
        self.status(STATUS_QUERY)
        log.info("options: %s searchlist:%s "%(options, searchlist))
        self._do_search(options, searchlist, 'name')

    @ExceptionHandler
    def search_details(self, options, search):
        '''
        Implement the {backend}-search-details functionality
        '''
        log.info("============= search_details ========")
        self.allow_cancel(True)
        #self.percentage(None)
        self.status(STATUS_QUERY)
        log.info("options: %s searchlist:%s "%(options, search))
        self._do_search(options, search, 'details' )


    @ExceptionHandler
    def get_packages(self, filter ):
        self.allow_cancel(False)
        self.status(STATUS_QUERY)
        log.info("options: %s searchlist:%s "%(filter,"all"))
        self._do_search(filter, "", 'all' )


    def get_requires(self, filters, package_ids, recursive_text):
        pass

    @ExceptionHandler
    def get_depends(self, filters, package_ids, recursive_text):
        name, version, flavor, installed = self._findPackage(package_ids[0])

        if name:
            if installed == INFO_INSTALLED:
                self.error(ERROR_PACKAGE_ALREADY_INSTALLED, 'Package already installed')

            else:
                updJob, suggMap = self._get_package_update(name, version,
                                                           flavor)
                for what, need in suggMap:
                    package_id = self.get_package_id(need[0], need[1], need[2])
                    depInstalled = self.check_installed(need[0])
                    if depInstalled == INFO_INSTALLED:
                        self.package(package_id, INFO_INSTALLED, '')
                    else:
                        self.package(package_id, INFO_AVAILABLE, '')
        else:
            self.error(ERROR_PACKAGE_ALREADY_INSTALLED, 'Package was not found')

    @ExceptionHandler
    def get_files(self, package_ids):
        self.allow_cancel(True)
        self.percentage(None)
        self.status(STATUS_INFO)
        package_id = package_ids[0]
        def _get_files(troveSource, n, v, f):
            files = []
            trv = troveSource.getTrove(n, v, f)
            for (n, v, f) in [x for x in trv.iterTroveList(strongRefs=True)
                                if troveSource.hasTrove(*x)]:
                for (pathId, path, fileId, version, filename) in \
                    troveSource.iterFilesInTrove(n, v, f, sortByPath = True,
                            withFiles=True, capsules=False):
                    files.append(path)
            return files

        for package in package_id.split("&"):
            log.info(package)
            name, version, flavor, installed = self._findPackage(package)

            if installed == INFO_INSTALLED:
                files = _get_files(self.client.db, name, version, flavor)
            else:
                files = _get_files(self.client.repos, name, version, flavor)

            self.files(package_id, ';'.join(files))

    @ExceptionHandler
    def update_system(self, only_trusted):

        # FIXME: use only_trusted

        self.allow_cancel(True)
        self.status(STATUS_UPDATE)
        self.client.setUpdateCallback( UpdateSystemCallback(self, self.cfg) )
        updateItems = self.client.fullUpdateItemList()
        [ log.info(i) for i,ver,flav in updateItems]
        applyList = [ (x[0], (None, None), x[1:], True) for x in updateItems ]

        updJob, suggMap = self._build_update_job(applyList)
        jobs = self._do_update(updJob)
        log.info(jobs)
        self.client.setUpdateCallback(self.callback )

#    @ExceptionHandler
    def refresh_cache(self, force):
        # TODO: use force ?

        #log.debug("refresh-cache command ")
    #    self.percentage()

        self.percentage(None)
        self.status(STATUS_REFRESH_CACHE)
        self.percentage(None)
        self.xmlcache.refresh()

    def install_packages(self, only_trusted, package_ids, simulate=False):
        """
            alias of update_packages
        """

        # FIXME: use only_trusted

        self.update_packages(only_trusted, package_ids, simulate)

    @ExceptionHandler
    def update_packages(self, only_trusted, package_ids, simulate=False):
        '''
        Implement the {backend}-{install, update}-packages functionality
        '''

        # FIXME: use only_trusted

        for package_id in package_ids:
            name, version, flavor, installed = self._findPackage(package_id)
            log.info((name, version, flavor, installed ))

            self.allow_cancel(True)
            self.percentage(0)
            self.status(STATUS_RUNNING)

            if name:
                """
                if installed == INFO_INSTALLED:
                    self.error(ERROR_PACKAGE_ALREADY_INSTALLED,
                        'Package already installed')
                """
                self.status(STATUS_INSTALL)
                self._do_package_update(name, version, flavor, simulate)


    @ExceptionHandler
    def remove_packages(self, allowDeps, autoremove, package_ids, simulate=False):
        '''
        Implement the {backend}-remove-packages functionality
        '''
        # TODO: use autoremove
        self.allow_cancel(True)
        self.percentage(0)
        self.status(STATUS_RUNNING)
        log.info("========== Remove Packages ============ ")
        log.info( allowDeps )
        self.client.setUpdateCallback(RemoveCallback(self, self.cfg))
        errors = ""
        #for package_id in package_ids.split('%'):
        for package_id in package_ids:
            name, version, arch,data = split_package_id(package_id)
            troveTuple = self.conary.query(name)
            for name,version,flavor in troveTuple:
                name = '-%s' % name
                #self.client.repos.findTrove(self.conary.default_label)
                self.status(STATUS_REMOVE)

                callback = self.client.getUpdateCallback()
                if callback.error:
                    self.error(ERROR_DEP_RESOLUTION_FAILED,', '.join(callback.error))

                self._do_package_update(name, version, flavor, simulate)
        self.client.setUpdateCallback(self.callback)

    def _get_metadata(self, package_id, field):
        '''
        Retrieve metadata from the repository and return result
        field should be one of:
                bibliography
                url
                notes
                crypto
                licenses
                shortDesc
                longDesc
                categories
        '''

        self.allow_cancel(True)
        self.percentage(None)
        self.status(STATUS_QUERY)
        n, v, f = self.get_package_from_id(package_id)
        trvList = self.client.repos.findTrove(self.cfg.installLabelPath,
                                     (n, v, f),
                                     defaultFlavor = self.cfg.flavor)

        troves = self.client.repos.getTroves(trvList, withFiles=False)
        result = ''
        for trove in troves:
            result = trove.getMetadata()[field]
        return result

    def _get_update_extras(self, package_id):
        notice = self._get_metadata(package_id, 'notice') or " "
        urls = {'jira':[], 'cve' : [], 'vendor': []}
        if notice:
            # Update Details
            desc = notice['description']
            # Update References (Jira, CVE ...)
            refs = notice['references']
            if refs:
                for ref in refs:
                    typ = ref['type']
                    href = ref['href']
                    title = ref['title']
                    if typ in ('jira', 'cve') and href != None:
                        if title == None:
                            title = ""
                        urls[typ].append("%s;%s" % (href, title))
                    else:
                        urls['vendor'].append("%s;%s" % (ref['href'], ref['title']))

            # Reboot flag
            if notice.get_metadata().has_key('reboot_suggested') and notice['reboot_suggested']:
                reboot = 'system'
            else:
                reboot = 'none'
            return _format_str(desc), urls, reboot
        else:
            return "", urls, "none"

    def _check_for_reboot(self, name):
        if name in self.rebootpkgs:
            self.require_restart(RESTART_SYSTEM, "")

    @ExceptionHandler
    def get_update_detail(self, package_ids):
        '''
        Implement the {backend}-get-update_detail functionality
        '''
        self.allow_cancel(True)
        self.percentage(None)
        self.status(STATUS_INFO)
        for package_id in package_ids:
            log.info(package_id)
            name, version,arch,summary  = get_package_from_id(package_id)
            pkgDict = self.xmlcache.resolve(name)
            update = ""
            obsolete = ""
            cve_url = ""
            if pkgDict:
                vendor_url = pkgDict.get("url","")
                desc = pkgDict.get("longDesc","")
                reboot = self._get_restart(pkgDict.get("name"))
                state = self._get_branch( pkgDict.get("label"))
                bz_url = self._get_fits(pkgDict.get("label"), pkgDict.get("name"))
                self.update_detail(package_id, update, obsolete, vendor_url, bz_url, cve_url,
                        reboot, desc, changelog="", state= state, issued="", updated = "")

   # @ExceptionHandler
    def get_details(self, package_ids):
        '''
        Print a detailed description for a given package
        '''
        self.allow_cancel(True)
        self.percentage(None)
        self.status(STATUS_INFO)

        log.info("========== get_details =============")
        for package_id in package_ids:
            name,version,arch,data = get_package_from_id(package_id)
            pkgDict = self.xmlcache.resolve(name)
            if name and pkgDict:
                longDesc = ""
                url = ""
                categories  = None
                license = ""

                longDesc = pkgDict.get("longDesc", "")
                url = pkgDict.get("url", "")
                categories = self.xmlcache.getGroup(pkgDict.get("category",""))
                license = self._get_license(pkgDict.get("licenses",""))
                size = pkgDict.get("size", 0)
                log.info("Details: %s, %s, %s, %s, %s, %d" % (package_id, license, categories, longDesc, url, size))
                self.details(package_id, license, categories, longDesc, url, size)

    def _show_package(self, name, version, flavor, status):
        '''  Show info about package'''
        log.info(name)
        package_id = self.get_package_id(name, version, flavor)
        summary = package_id.split(";")
        meta = summary[3]

        self.package(package_id, status, meta)

    def _get_restart(self, name):
        if name in self.rebootpkgs:
            return RESTART_SYSTEM
        elif name in self.restartpkgs:
            return RESTART_APPLICATION
        else:
            return RESTART_NONE

    def _get_info(self, name):
        if name in self.rebootpkgs:
            return INFO_SECURITY
        elif name in self.restartpkgs:
            return INFO_SECURITY
        else:
            return INFO_NORMAL


    def _get_status(self, notice):
        if name in self.rebootpkgs:
            return INFO_SECURITY
        elif name in self.restartpkgs:
            return INFO_INSTALLED
        else:
            return INFO_NORMAL
    def _get_fits(self, branch, pkg_name):
        if "conary.rpath.com" in branch:
            return "http://issues.rpath.com;rPath Issues Tracker"
        elif "foresight.rpath.org" in branch:
            return "http://issues.foresightlinux.org; Foresight Issues Tracker"
        else:
            return ""
    def _get_license(self, license_list ):
        if license_list == "":
            return ""

        # license_list is a list of licenses in the format of
        # 'rpath.com/licenses/copyright/GPL-2'.
        return " ".join([i.split("/")[-1] for i in license_list])

    def _upgrade_from_branch( self, branch):
        branchList = branch.split("@")
        if "2-qa" in branchList[1]:
            return DISTRO_UPGRADE_TESTING
        elif "2-devel" in branchList[1]:
            return DISTRO_UPGRADE_UNSTABLE
        else:
            return DISTRO_UPGRADE_STABLE


    def _get_branch(self, branch ):
        branchList = branch.split("@")
        if "2-qa" in branchList[1]:
            return UPDATE_STATE_TESTING
        elif "2-devel" in branchList[1]:
            return UPDATE_STATE_UNSTABLE
        else:
            return UPDATE_STATE_STABLE
    @ExceptionHandler
    def get_updates(self, filters):
        self.allow_cancel(True)
        self.percentage(0)
        self.status(STATUS_INFO)

        getUpdateC= GetUpdateCallback(self,self.cfg)
        self.client.setUpdateCallback(getUpdateC)

        log.info("============== get_updates ========================")
        log.info("get fullUpdateItemList")
        updateItems =self.client.fullUpdateItemList()
#        updateItems = cli.cli.getUpdateItemList()
        applyList = [ (x[0], (None, None), x[1:], True) for x in updateItems ]

        self.status(STATUS_RUNNING)
        updJob, suggMap = self._build_update_job(applyList)

        log.info("getting JobLists...........")
        r = []
        for num, job in enumerate(updJob.getJobs()):
            name = job[0][0]

            # On an erase display the old version/flavor information.
            version = job[0][2][0]
            if version is None:
                version = job[0][1][0]

            flavor = job[0][2][1]
            if flavor is None:
                flavor = job[0][1][1]

            info = self._get_info(name)
            trove_info = ( ( name,version,flavor ), info)
            r.append(trove_info)

        pkg_list = self.xmlcache.resolve_list([ name for (  ( name,version,flavor), info )  in r ])
        log.info("generate the pkgs ")
        new_res = []
        for pkg in pkg_list:
            for ( trove, info ) in r:
                #log.info( ( pkg, trove) )
                name,version,flav = trove
                if name == pkg["name"]:
                    npkg = self._convert_package( trove, pkg)
                    new_res.append( ( npkg, info ) )

        log.info(new_res)

        self._show_package_list(new_res)
        log.info("============== end get_updates ========================")
        self.client.setUpdateCallback(self.callback)

    def _findPackage(self, package_id):
        '''
        find a package based on a package id (name;version;arch;summary)
        '''
        log.info("========== _findPackage ==========")
        log.info(package_id)
        troveTuples = self.get_package_from_id(package_id)
        log.info(troveTuples)
        for troveTuple in troveTuples:
            log.info("======== trove ")
            log.info(troveTuple)
            installed = self.check_installed(troveTuple)
            log.info(installed)
            name, version, flavor = troveTuple
            return name, version, flavor, installed
        else:
            self.error(ERROR_INTERNAL_ERROR, "package_id Not Correct ")

    def repo_set_data(self, repoid, parameter, value):
        '''
        Implement the {backend}-repo-set-data functionality
        '''
        pass

    def get_repo_list(self, filters):
        '''
        Implement the {backend}-get-repo-list functionality
        '''
        log.info("======= get repo list ===========0")
        labels = self.conary.get_labels_from_config()
        self.status(STATUS_QUERY)
        for repo in labels:
            repo_name = repo.split("@")[0]
            repo_branch  = repo.split("@")[1]
            self.repo_detail(repo,repo,True)

    def repo_enable(self, repoid, enable):
        '''
        Implement the {backend}-repo-enable functionality
        '''
        pass

    def simulate_install_packages(self, package_ids):
	'''
	Simulate an install of one or more packages.
        '''
	return self.install_packages(False, package_ids, simulate=True)

    def simulate_update_packages(self, package_ids):
	'''
	Simulate an update of one or more packages.
        '''
	return self.update_packages(False, package_ids, simulate=True)

    def simulate_remove_packages(self, package_ids):
	'''
	Simulate an update of one or more packages.
        '''
	return self.remove_packages(False, False, package_ids, simulate=True)

def main():
    backend = PackageKitConaryBackend('')
    log.info("======== argv =========== ")
    log.info(sys.argv)
    backend.dispatcher(sys.argv[1:])

if __name__ == "__main__":
    main()
