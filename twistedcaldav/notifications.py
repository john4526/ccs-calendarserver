##
# Copyright (c) 2006 Apple Computer, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# DRI: Cyrus Daboo, cdaboo@apple.com
##

from twisted.internet.defer import deferredGenerator
from twisted.internet.defer import waitForDeferred
from twisted.web2.dav.method import put_common
from twisted.web2.dav.resource import DAVPrincipalResource
from twisted.web2.dav import davxml

from twistedcaldav import customxml
from twistedcaldav.customxml import apple_namespace
from twistedcaldav.extensions import DAVFile
from twistedcaldav.extensions import DAVResource

import datetime
import md5
import os
import time

"""
Implements collection change notification functionality. Any change to the contents of a collection will
result in a notification resource deposited into subscriber's notifications collection.
"""

__all__ = [
    "Notification",
    "NotificationResource",
    "NotificationFile",
]

class Notification(object):
    """
    Encapsulates a notification message.
    """
    
    ACTION_NONE        = 0
    ACTION_CREATED     = 1
    ACTION_MODIFIED    = 2
    ACTION_DELETED     = 3
    ACTION_COPIED_TO   = 4
    ACTION_COPIED_FROM = 5
    ACTION_MOVED_TO    = 6
    ACTION_MOVED_FROM  = 7

    def __init__(self, action, authid=None, oldETag=None, newETag=None, oldURI=None, newURI=None):
        self.action = action
        self.timestamp = datetime.datetime.utcnow()
        self.authid = authid
        self.oldETag = oldETag
        self.newETag = newETag
        self.oldURI = oldURI
        self.newURI = newURI

    def doNotification(self, request, parent, resource):
        """
        Put the supplied noitification into the notification collection of the specified principal.
        
        @param request: L{Request} for request in progress.
        @param resource: L{DAVResource}trigerring the notification.
        """
        
        # First determine which principals should get notified
        #
        # Procedure:
        #
        # 1. Get the list of auto-subscribed principals from the parent collection property.
        # 2. Expand any group principals in the list into their user principals.
        # 3. Get the list of unsubscribed principals from the parent collection property.
        # 4. Expand any group principals in the list into their user principals.
        # 5. Generate a set from the difference between the subscribed list and unsubscribed list.
        
        def _expandPrincipals(principals):
            result = []
            for principal in principals:

                principal = waitForDeferred(parent.resolvePrincipal(principal.children[0], request))
                yield principal
                principal = principal.getResult()
                if principal is None:
                    continue
        
                presource = waitForDeferred(request.locateResource(str(principal)))
                yield presource
                presource = presource.getResult()
        
                if not isinstance(presource, DAVPrincipalResource):
                    continue
                
                # Step 2. Expand groups.
                members = presource.groupMembers()
                
                if members:
                    for member in members:
                        result.append(davxml.Principal(davxml.HRef.fromString(member)))
                else:
                    result.append(davxml.Principal(principal))
            yield result

        _expandPrincipals = deferredGenerator(_expandPrincipals)

        # For drop box we look at the parent collection of the target resource and get the
        # set of subscribed principals.
        if not parent.hasDeadProperty(customxml.Subscribed):
            yield None
            return

        principals = set()
        autosubs = parent.readDeadProperty(customxml.Subscribed).children
        d = waitForDeferred(_expandPrincipals(autosubs))
        yield d
        autosubs = d.getResult()
        principals.update(autosubs)
        
        for principal in principals:
            if not isinstance(principal.children[0], davxml.HRef):
                continue
            purl = str(principal.children[0])
            d = waitForDeferred(request.locateResource(purl))
            yield d
            presource = d.getResult()

            collectionURL = presource.notificationsURL()
            if collectionURL is None:
                continue
            d = waitForDeferred(request.locateResource(collectionURL))
            yield d
            collection = d.getResult()

            name = "%s.xml" % (md5.new(str(self) + str(time.time()) + collectionURL).hexdigest(),)
            path = os.path.join(collection.fp.path, name)
    
            # Create new resource in the collection
            child = NotificationFile(path=path)
            collection.putChild(name, child)
            d = waitForDeferred(request.locateChildResource(collection, name))    # This ensures the URI for the resource is mapped
            yield d
            child = d.getResult()

            d = waitForDeferred(child.create(request, self))
            yield d
            d.getResult()
        
    doNotification = deferredGenerator(doNotification)

class NotificationResource(DAVResource):
    """
    Resource that gets stored in a notification collection and which contains
    the notification details in its content as well as via properties.
    """

    liveProperties = DAVResource.liveProperties + (
        (apple_namespace, "action"      ),
        (apple_namespace, "time-stamp"  ),
        (apple_namespace, "auth-id"     ),
        (apple_namespace, "old-uri"     ),
        (apple_namespace, "new-uri"     ),
        (apple_namespace, "old-etag"    ),
        (apple_namespace, "new-etag"    ),
    )

class NotificationFile(DAVResource, DAVFile):

    def __init__(self, path):
        super(NotificationFile, self).__init__(path)

    def create(self, request, notification):
        """
        Create the resource, fill out the body, and add properties.
        """
        
        # Create body XML
        elements = []
        elements.append(customxml.Action(
            {
                Notification.ACTION_CREATED:     customxml.Created(),
                Notification.ACTION_MODIFIED:    customxml.Modified(),
                Notification.ACTION_DELETED:     customxml.Deleted(),
                Notification.ACTION_COPIED_TO:   customxml.CopiedTo(),
                Notification.ACTION_COPIED_FROM: customxml.CopiedFrom(),
                Notification.ACTION_MOVED_TO:    customxml.MovedTo(),
                Notification.ACTION_MOVED_FROM:  customxml.MovedFrom(),
            }[notification.action]
            ))

        elements.append(customxml.TimeStamp.fromString(notification.timestamp))
        if notification.authid:
            elements.append(customxml.AuthID.fromString(notification.authid))
        if notification.oldURI:
            elements.append(customxml.OldURI(davxml.HRef.fromString(notification.oldURI)))
        if notification.newURI:
            elements.append(customxml.NewURI(davxml.HRef.fromString(notification.newURI)))
        if notification.oldETag:
            elements.append(customxml.OldETag.fromString(notification.oldETag))
        if notification.newETag:
            elements.append(customxml.NewETag.fromString(notification.newETag))
                          
        xml = customxml.Notification(*elements)
        
        d = waitForDeferred(put_common.storeResource(request, data=xml.toxml(), destination=self, destination_uri=request.urlForResource(self)))
        yield d
        d.getResult()

        # Write properties
        for element in elements:
            self.writeDeadProperty(element)

    create = deferredGenerator(create)
