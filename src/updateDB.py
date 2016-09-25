import urlparse
import urllib
from mimetypes import guess_all_extensions
from datetime import datetime
import re
import os
import sys
import time
from collections import OrderedDict
import threading

from PySide.QtWebKit import QWebView, QWebPage
from PySide.QtGui import QMessageBox
import requests
from requests.exceptions import *
from rauth import OAuth1Service
import dateutil.parser

from paramedit import *

from utilities import *
from credentials import *

class update():

    def __init__(self, mainWindow=None, name="NoName"):
        self.timeout = None
        self.mainWindow = mainWindow
        self.name = name
        self.connected = False
        self.lastrequest = None
        self.loadDocs()
        self.lock_session = threading.Lock()

    def parseURL(self, url):
        """
        Parse any url and return the query-strings and base bath
        """
        url = url.split('?', 1)
        path = url[0]
        query = url[1] if len(url) > 1 else ''
        query = urlparse.parse_qsl(query)
        query = OrderedDict((k, v) for k, v in query)

        return path, query

    def parsePlaceholders(self,pattern,nodedata,paramdata={}):
        if not pattern:
            return pattern

        matches = re.findall(ur"<([^>]*)>", pattern)
        for match in matches:
            if match in paramdata:
                value = paramdata[match]
            elif match == 'None':
                value = ''
            elif match == 'Object ID':
                value = unicode(nodedata['objectid'])
            else:
                value = getDictValue(nodedata['response'], match)

            pattern = pattern.replace('<' + match + '>', value)

        return pattern

    def getURL(self, urlpath, params, nodedata):
        """
        Replaces the Facepager placeholders ("<",">" of the inside the query-Parameter
        by the Object-ID or any other Facepager-Placeholder
        Example: http://www.facebook.com/<Object-ID>/friends
        """
        urlpath, urlparams = self.parseURL(urlpath)

        #Replace placeholders in params and collect template params
        templateparams = {}
        for name in params:
            #Filter empty params
            if (name == '<None>') or (params[name] == '<None>') or (name == ''):
                continue

            # Set the value for the ObjectID or any other placeholder-param
            if params[name] == '<Object ID>':
                value = unicode(nodedata['objectid'])
            else:
                match = re.match(ur"^<(.*)>$", unicode(params[name]))
                if match:
                    value = getDictValue(nodedata['response'], match.group(1))
                else:
                    value = params[name]

            #check for template params
            match = re.match(ur"^<(.*)>$", unicode(name))
            if match:
                templateparams[match.group(1)] = value
            else:
                urlparams[name] = unicode(value).encode("utf-8")

        #Replace placeholders in urlpath
        urlpath = self.parsePlaceholders(urlpath, nodedata, templateparams)

        return urlpath, urlparams


    def getOptions(self, purpose='fetch'): #purpose = 'fetch'|'settings'|'preset'
        return {}

    def setOptions(self, options):
        if options.has_key('client_id'):
            self.clientIdEdit.setText(options.get('client_id',''))
        if 'access_token' in options:
            self.tokenEdit.setText(options.get('access_token', ''))
        if 'access_token_secret' in options:
            self.tokensecretEdit.setText(options.get('access_token_secret', ''))
        if options.has_key('consumer_key'):
            self.consumerKeyEdit.setText(options.get('consumer_key',''))
        if options.has_key('twitter_consumer_secret'):
            self.consumerSecretEdit.setText(options.get('consumer_secret',''))

    def saveSettings(self):
        self.mainWindow.settings.beginGroup("ApiModule_" + self.name)
        options = self.getOptions('settings')

        for key in options.keys():
            self.mainWindow.settings.setValue(key, options[key])
        self.mainWindow.settings.endGroup()

    def loadSettings(self):
        self.mainWindow.settings.beginGroup("ApiModule_" + self.name)

        options = {}
        for key in self.mainWindow.settings.allKeys():
            options[key] = self.mainWindow.settings.value(key)
        self.mainWindow.settings.endGroup()
        self.setOptions(options)

    def loadDocs(self):
        '''
        Loads and prepares documentation
        '''

        try:
            if getattr(sys, 'frozen', False):
                folder = os.path.join(os.path.dirname(sys.executable),'docs')
            elif __file__:
                folder = os.path.join(os.path.dirname(__file__),'docs')

            filename = u"{0}.json".format(self.__class__.__name__)

            with open(os.path.join(folder, filename),"r") as docfile:
                if docfile:
                    self.apidoc = json.load(docfile)
                else:
                    self.apidoc = None
        except:
            self.apidoc = None

    def setRelations(self,params=True):
        '''
        Create relations box and paramedit
        Set the relations according to the APIdocs, if any docs are available
        '''

        self.relationEdit = QComboBox(self)
        if self.apidoc:
            #Insert one item for every endpoint
            for endpoint in reversed(self.apidoc):
                #store url as item text
                self.relationEdit.insertItem(0, endpoint["path"])
                #store doc as tooltip
                self.relationEdit.setItemData(0, endpoint["doc"], Qt.ToolTipRole)
                #store params-dict for later use in onChangedRelation
                self.relationEdit.setItemData(0, endpoint.get("params",[]), Qt.UserRole)

        self.relationEdit.setEditable(True)
        if params:
            self.paramEdit = QParamEdit(self)
            # changed to currentIndexChanged for recognition of changes made by the tool itself
            self.relationEdit.currentIndexChanged.connect(self.onchangedRelation)
            self.onchangedRelation()

    @Slot()
    def onchangedRelation(self,index=0):
        '''
        Handles the automated paramter suggestion for the current
        selected API Relation/Endpoint
        '''
        #retrieve param-dict stored in setRelations-method
        params = self.relationEdit.itemData(index,Qt.UserRole)

        #Set name options and build value dict
        values = {}
        nameoptions = []
        if params:
            for param in params:
                if param["required"]==True:
                    nameoptions.append(param)
                    values[param["name"]] = param["default"]
                else:
                    nameoptions.insert(0,param)
        nameoptions.insert(0,{})
        self.paramEdit.setNameOptions(nameoptions)

        #Set value options
        self.paramEdit.setValueOptions([{'name':'',
                                         'doc':"No Value"},
                                         {'name':'<Object ID>',
                                          'doc':"The value in the Object ID-column of the datatree."}])

        #Set values
        self.paramEdit.setParams(values)

    @Slot()
    def onChangedParam(self,index=0):
        pass

    def initSession(self):
        self.session = requests.Session()
        return self.session

    def request(self, path, args=None, headers=None, jsonify=True,speed=None):
        """
        Start a new threadsafe session and request
        """


        self.lastrequest = QDateTime.currentDateTime()

        session = self.initSession()

        try:
            maxretries = 3
            while True:
                try:
                    if headers is not None:
                        response = session.post(path, params=args, headers=headers, timeout=self.timeout, verify=False)
                    else:
                        response = session.get(path, params=args, timeout=self.timeout, verify=False)
                except (HTTPError, ConnectionError), e:
                    maxretries -= 1
                    if maxretries > 0:
                        sleep(0.1)
                        self.mainWindow.logmessage(u"Automatic retry: Request Error: {0}".format(e.message))
                    else:
                        raise e
                else:
                    break

        except (HTTPError, ConnectionError), e:
            raise Exception(u"Request Error: {0}".format(e.message))
        else:
            if jsonify == True:
                if not response.json():
                    raise Exception("Request Format Error: No JSON data!")

                else:
                    status = 'fetched' if response.ok else 'error'
                    status = status + ' (' + str(response.status_code) + ')'
                    return response.json(), dict(response.headers.items()), status
            else:
                return response

    def disconnectSocket(self):
        """Used to disconnect when canceling requests"""
        self.connected = False




    def getOptions(self, purpose='fetch'):  # purpose = 'fetch'|'settings'|'preset'
        options = {'relation': self.relationEdit.currentText(), 'pages': self.pagesEdit.value(),
                   'params': self.paramEdit.getParams()}

        options['scope'] = self.scopeEdit.text()
        options['basepath'] = self.basepathEdit.currentText()
        #options['folder'] = self.folderEdit.text()

        # options for request
        if purpose != 'preset':
            options['querytype'] = self.name + ':' + self.relationEdit.currentText()
            options['access_token'] = self.tokenEdit.text()
            options['client_id'] = self.clientIdEdit.text()


        # options for data handling
        if purpose == 'fetch':
            options['objectid'] = 'id'
            options['nodedata'] = 'data' if ('/' in options['relation']) or (options['relation'] == 'search') else None

        return options

    def setOptions(self, options):
        #define default values
        if options.get('basepath','') == '':
            options['basepath']= "https://graph.facebook.com/v2.2/"
        if options.get('scope','') == '':
            options['scope']= "user_groups"

        #set values
        self.relationEdit.setEditText(options.get('relation', '<page>'))
        self.pagesEdit.setValue(int(options.get('pages', 1)))

        self.basepathEdit.setEditText(options.get('basepath'))
        self.scopeEdit.setText(options.get('scope'))
        self.paramEdit.setParams(options.get('params', {}))

        # set Access-tokens,use generic method from APITab
        super(FacebookTab, self).setOptions(options)

    def fetchData(self, nodedata, options=None, callback=None):
    # Preconditions
        if options['access_token'] == '':
            raise Exception('Access token is missing, login please!')
        self.connected = True

        # Abort condition for time based pagination
        since = options['params'].get('since', False)
        if (since != False):
            since = dateutil.parser.parse(since, yearfirst=True, dayfirst=False)
            since = int((since - datetime(1970, 1, 1)).total_seconds())

        # Abort condition: maximum page count
        for page in range(0, options.get('pages', 1)):
        # build url
            if not ('url' in options):
                urlpath = options["basepath"] + options['relation']
                urlparams = {}

                if options['relation'] == 'search':
                    urlparams['q'] = self.idtostr(nodedata['objectid'])
                    urlparams['type'] = 'page'

                elif options['relation'] == '<Object ID>':
                    urlparams['metadata'] = '1'

                elif '<Object ID>/' in options['relation']:
                    urlparams['limit'] = '100'

                urlparams.update(options['params'])

                urlpath, urlparams = self.getURL(urlpath, urlparams, nodedata)
                urlparams["access_token"] = options['access_token']
            else:
                urlpath = options['url']
                urlparams = options['params']


            # data
            options['querytime'] = str(datetime.now())
            data, headers, status = self.request(urlpath, urlparams,None,True,options.get('speed',None) )
            options['querystatus'] = status

            return data

    @Slot()
    def getToken(self):
        url = urlparse.parse_qs(self.login_webview.url().toString())
        if "https://www.facebook.com/connect/login_success.html#access_token" in url:
            token = url["https://www.facebook.com/connect/login_success.html#access_token"]
            if token:
                self.tokenEdit.setText(token[0])
                self.login_webview.parent().close()
