/**
 * Builds full page project browser
 */
'use strict';

var Treebeard = require('treebeard');   // Uses treebeard, installed as plugin
var $ = require('jquery');  // jQuery
var m = require('mithril'); // exposes mithril methods, useful for redraw etc.
var ProjectOrganizer = require('js/project-organizer');
var $osf = require('js/osfHelpers');

var LinkObject = function (type, data, label, index) {
    var self = this;
    self.id = getUID();
    self.type = type;
    self.data = data;
    self.label = label;
    self.index = index;  // For breadcrumbs to cut off when clicking parent level
    self.generateLink = function () {
        if (self.type === 'collection'){
            return $osf.apiV2Url(self.data.path, {
                    query : self.data.query
                }
            );
        }
        else if (self.type === 'tag') {
            return $osf.apiV2Url('nodes/', { query : {'filter[tags]' : self.data.tag , 'related_counts' : true}});
        }
        else if (self.type === 'name') {
            return $osf.apiV2Url('users/' + self.data.id + '/nodes/', { query : {'related_counts' : true}});
        }
        else if (self.type === 'node') {
            return $osf.apiV2Url('nodes/' + self.data.uid + '/children/', { query : { 'related_counts' : true }});
        }
        // If nothing
        throw new Error('Link could not be generated from linkObject data');
    };
    self.link = self.generateLink();
};

var Activity = function(date, text, meta) {
    this.date = date;
    this.text = text;
    this.meta = meta;
};

if (!window.fileBrowserCounter) {
    window.fileBrowserCounter = 0;
}
function getUID() {
    window.fileBrowserCounter = window.fileBrowserCounter + 1;
    return window.fileBrowserCounter;
}

/**
 * Initialize File Browser. Prepeares an option object within FileBrowser
 * @constructor
 */
var FileBrowser = {
    controller : function (options) {
        var self = this;
        self.wrapperSelector = options.wrapperSelector;  // For encapsulating each implementation of this component in multiple use
        self.currentLink = ''; // Save the link to compare if a new link is being requested and avoid multiple calls
        self.reload = m.prop(false); // Gets set to true when treebeard link changes and it needs to be redrawn
        self.nonLoadTemplate = m.prop(m('', 'Loading')); // Template for when data is not available or error happens

        // VIEW STATES
        self.showInfo = m.prop(false); // Show the info panel
        self.showSidebar = m.prop(true); // Show the links with collections etc. used in narrow views
        self.showCollectionMenu = m.prop(false); // Show hide ellipsis menu for collections
        self.collectionMenuObject = m.prop({item : {label:null}, x : 0, y : 0}); // Collection object to complete actions on menu

        // DEFAULT DATA -- to be switched with server response
        self.collections = [
            new LinkObject('collection', { path : 'users/me/nodes/', query : { 'related_counts' : true }, systemCollection : true}, 'All My Projects'),
            new LinkObject('collection', { path : 'registrations/', query : { 'related_counts' : true }, systemCollection : true}, 'All My Registrations'),
            new LinkObject('collection', { path : 'users/me/nodes/', query : { 'related_counts' : true }, systemCollection : false}, 'Everything'),
        ];
        self.breadcrumbs = m.prop([
            new LinkObject('collection', { path : 'users/me/nodes/', query : { 'related_counts' : true }, systemCollection : true}, 'All My Projects'),
        ]);
        self.nameFilters = [
            new LinkObject('name', { id : '8q36f', query : { 'related_counts' : true }}, 'Caner Uguz'),
        ];
        self.tagFilters = [
            new LinkObject('tag', { tag : 'something', query : { 'related_counts' : true }}, 'Something Else'),
        ];
        self.data = m.prop([]);
        self.activityLogs = m.prop([
            new Activity('2015-08-19 01:34 PM', 'Caner Uguz added a comment to Dashboard Redesign Proposal', {}),
        ]);

        self.updateFilesData = function(linkObject) {
            if (linkObject.link !== self.currentLink) {
                self.updateBreadcrumbs(linkObject);
                self.updateList(linkObject.link);
                self.currentLink = linkObject.link;
            }
            self.showSidebar(false);
        };

        // INFORMATION PANEL
        self.selected = m.prop([]);
        self.updateSelected = function(selectedList){
            // If single project is selected, get activity
            if(selectedList.length === 1){
                self.activityLogs([
                    new Activity('2015-08-19 01:34 PM', 'Caner Uguz added a comment to Dashboard Redesign Proposal', {}),
                    new Activity('2015-08-19 01:34 PM', 'John Chandler added a page to Dashboard Redesign Proposal wiki', {})
                ]);
            }
            self.selected(selectedList);
        };

        // USER FILTER
        self.activeFilter = m.prop(1);
        self.updateFilter = function(filter) {
            self.activeFilter(filter.data.id);
            self.updateFilesData(filter);
        };


        // Refresh the Grid
        self.updateList = function(url){
            var xhrconfig = function (xhr) {
                xhr.withCredentials = true;
            };
            m.request({method : 'GET', url : url, config : xhrconfig})
                .then(function(value){
                    value.data.map(function(item){
                        item.kind = 'folder';
                        item.uid = item.id;
                        item.name = item.attributes.title;
                        item.date = new $osf.FormattableDate(item.attributes.date_modified);

                        // TODO: Dummy data, remove this when api is ready
                        item.contributors = [{
                            id: '8q36f',
                            name : 'Dummy User'
                        }];
                    });
                self.data(value);
                self.reload(true);
            }, function(result){
                    self.nonLoadTemplate(m('.fb-error.text-danger', [
                        m('p','Projects couldn\'t load.'),
                        m('p', m('.btn.btn-link', { onclick : self.updateFilter.bind(null, self.collections[0])},' Reload \'All My Projects\''))
                    ]));
                    console.error(result);
                    throw new Error('Receiving initial data for File Browser failed. Please check your url');
                });
        };


        // BREADCRUMBS
        self.updateBreadcrumbs = function(linkObject){
            if (linkObject.type === 'collection' || linkObject.type === 'name' || linkObject.type === 'tag'){
                self.breadcrumbs([linkObject]);
                return;
            }
            if (linkObject.placement === 'breadcrumb'){
                self.breadcrumbs().splice(linkObject.index+1, self.breadcrumbs().length-linkObject.index-1);
                return;
            }
            if(linkObject.ancestors.length > 0){
                linkObject.ancestors.forEach(function(item){
                    var ancestorLink = new LinkObject('node', item.data, item.data.name);
                    self.breadcrumbs().push(ancestorLink);
                });
            }
            self.breadcrumbs().push(linkObject);
        }.bind(self);

        self.sidebarInit = function (element, isInit) {
            if(!isInit){
                $('[data-toggle="tooltip"]').tooltip();
            }
        };

        self.updateCollectionMenu = function (item, event) {
            var x = event.x - 4;
            var y = event.y-275;
            if (event.view.innerWidth < 767){
                x = x-115;
                y = y-80;
            }
            self.showCollectionMenu(true);
            self.collectionMenuObject({
                item : item,
                x : x,
                y : y
            });
        };
        self.init = function () {
            var loadUrl = $osf.apiV2Url(self.collections[0].data.path, {
                query : self.collections[0].data.query
            });
            self.updateList(loadUrl);
        };
        self.init();
    },
    view : function (ctrl) {
        var mobile = window.innerWidth < 767; // true if mobile view
        var infoPanel = '';
        var poStyle = 'width : 75%';
        var infoButtonClass = 'btn-default';
        var sidebarButtonClass = 'btn-default';
        if (ctrl.showInfo() && !mobile){
            infoPanel = m('.fb-infobar', m.component(Information, { selected : ctrl.selected, activityLogs : ctrl.activityLogs  }));
            infoButtonClass = 'btn-primary';
            poStyle = 'width : 45%';
        }
        if(ctrl.showSidebar()){
            sidebarButtonClass = 'btn-primary';
        }
        if (mobile) {
            poStyle = 'width : 100%';
        } else {
            ctrl.showSidebar(true);
        }
        return [
            m('.fb-header.m-b-xs.row', [
                m('.col-xs-12.col-sm-6', m.component(Breadcrumbs, {
                    data : ctrl.breadcrumbs,
                    updateFilesData : ctrl.updateFilesData
                })),
                m('.fb-buttonRow.col-xs-12.col-sm-6', [
                    mobile ? m('button.btn.btn-sm.m-r-sm', {
                        'class' : sidebarButtonClass,
                        onclick : function () {
                            ctrl.showSidebar(!ctrl.showSidebar());
                        }
                    }, m('.fa.fa-bars')) : '',
                    m('#poFilter.m-r-xs'),
                    !mobile ? m('button.btn', {
                        'class' : infoButtonClass,
                        onclick : function () {
                            ctrl.showInfo(!ctrl.showInfo());
                        }
                    }, m('.fa.fa-info')) : ''
                ])
            ]),
            ctrl.showSidebar() ?
            m('.fb-sidebar', { config : ctrl.sidebarInit}, [
                mobile ? m('.fb-dismiss', m('button.close[aria-label="Close"]', {
                    onclick : function () {
                        ctrl.showSidebar(false);
                    }
                }, [
                    m('span[aria-hidden="true"]','×'),
                ])) : '',
                m.component(Collections, {
                    list : ctrl.collections,
                    activeFilter : ctrl.activeFilter,
                    updateFilter : ctrl.updateFilter,
                    showCollectionMenu : ctrl.showCollectionMenu,
                    updateCollectionMenu : ctrl.updateCollectionMenu,
                    collectionMenuObject : ctrl.collectionMenuObject
                }),
                m.component(Filters, {
                    activeFilter : ctrl.activeFilter,
                    updateFilter : ctrl.updateFilter,
                    nameFilters : ctrl.nameFilters,
                    tagFilters : ctrl.tagFilters
                })
            ]) : '',
            m('.fb-main', { style : poStyle },
                ctrl.data().length === 0 ? ctrl.nonLoadTemplate() : m('#poOrganizer',  m.component( ProjectOrganizer, {
                        filesData : ctrl.data,
                        updateSelected : ctrl.updateSelected,
                        updateFilesData : ctrl.updateFilesData,
                        LinkObject : LinkObject,
                        reload : ctrl.reload
                    })
                )
            ),
            infoPanel,
            m.component(Modals, { collectionMenuObject : ctrl.collectionMenuObject, selected : ctrl.selected}),
            mobile && ctrl.showSidebar() ? m('.fb-overlay') : ''
        ];
    }
};

/**
 * Collections Module.
 * @constructor
 */
var Collections  = {
    view : function (ctrl, args) {
        var selectedCSS;
        return m('.fb-collections', [
            m('h5', [
                'Collections ',
                m('i.fa.fa-question-circle.text-muted', {
                    'data-toggle':  'tooltip',
                    'title':  'Collections are groups of projects. You can create new collections and add any project you are a collaborator on or a public project.',
                    'data-placement' : 'bottom'
                }, ''),
                m('.pull-right', m('button.btn.btn-xs.btn-success[data-toggle="modal"][data-target="#addColl"]', m('i.fa.fa-plus')))
            ]),
            m('ul', [
                args.list.map(function(item){
                    selectedCSS = item.id === args.activeFilter() ? 'active' : '';
                    return m('li', { className : selectedCSS},
                        [
                            m('a', { href : '#', onclick : args.updateFilter.bind(null, item) },  item.label),
                            !item.data.systemCollection ? m('i.fa.fa-ellipsis-v.pull-right.text-muted.p-xs', {
                                onclick : function (e) {
                                    args.updateCollectionMenu(item, e);
                                }
                            }) : ''
                        ]
                    );
                }),
                args.showCollectionMenu() ? m('.collectionMenu',{
                    style : 'top: ' + args.collectionMenuObject().y + 'px;left: ' + args.collectionMenuObject().x + 'px;'
                }, [
                    m('ul', [
                        m('li[data-toggle="modal"][data-target="#renameColl"]',{
                            onclick : function (e) {
                                args.showCollectionMenu(false);
                            }
                        }, [
                            m('i.fa.fa-pencil'),
                            ' Rename'
                        ]),
                        m('li[data-toggle="modal"][data-target="#removeColl"]',{
                            onclick : function (e) {
                                args.showCollectionMenu(false);
                            }
                        }, [
                            m('i.fa.fa-trash'),
                            ' Delete'
                        ])
                    ])
                ]) : ''
            ]),

        ]);
    }
};

/**
 * Breadcrumbs Module.
 * @constructor
 */

var Breadcrumbs = {
    view : function (ctrl, args) {
        var mobile = window.innerWidth < 767; // true if mobile view
        var items = args.data();
        if (mobile && items.length > 1) {
            return m('.fb-breadcrumbs', [
                m('ul', [
                    m('li', [
                        m('.btn.btn-link[data-toggle="modal"][data-target="#parentsModal"]', '...'),
                        m('i.fa.fa-angle-right')
                    ]),
                    m('li', m('span.btn', items[items.length-1].label))
                ]),
                m('#parentsModal.modal.fade[tabindex=-1][role="dialog"][aria-hidden="true"]',
                    m('.modal-dialog',
                        m('.modal-content', [
                            m('.modal-body', [
                                m('button.close[data-dismiss="modal"][aria-label="Close"]', [
                                    m('span[aria-hidden="true"]','×'),
                                ]),
                                m('h4', 'Parent Projects'),
                                args.data().map(function(item, index, array){
                                    if(index === array.length-1){
                                        return m('.fb-parent-row.btn', {
                                            style : 'margin-left:' + (index*20) + 'px;',
                                        },  [
                                            m('i.fa.fa-angle-right.m-r-xs'),
                                            item.label
                                        ]);
                                    }
                                    item.index = index; // Add index to update breadcrumbs
                                    item.placement = 'breadcrumb'; // differentiate location for proper breadcrumb actions
                                    return m('.fb-parent-row',
                                        m('span.btn.btn-link', {
                                            style : 'margin-left:' + (index*20) + 'px;',
                                            onclick : function() {
                                                args.updateFilesData(item);
                                                $('.modal').modal('hide');
                                            }
                                        },  [
                                            m('i.fa.fa-angle-right.m-r-xs'),
                                            item.label
                                        ])
                                    );
                                })
                            ]),
                        ])
                    )
                )
            ]);
        }
        return m('.fb-breadcrumbs', m('ul', [
            args.data().map(function(item, index, array){
                if(index === array.length-1){
                    return m('li',  m('span.btn', item.label));
                }
                item.index = index; // Add index to update breadcrumbs
                item.placement = 'breadcrumb'; // differentiate location for proper breadcrumb actions
                return m('li',
                    m('span.btn.btn-link', { onclick : args.updateFilesData.bind(null, item)},  item.label),
                    m('i.fa.fa-angle-right')
                );
            })

        ]));
    }
};


/**
 * Filters Module.
 * @constructor
 */
var Filters = {
    view : function (ctrl, args) {
        var selectedCSS;
        return m('.fb-filters.m-t-lg',
            [
                m('h5', 'Contributors'),
                m('ul', [
                    args.nameFilters.map(function(item, index){
                        selectedCSS = item.id === args.activeFilter() ? '.active' : '';
                        return m('li' + selectedCSS,
                            m('a', { href : '#', onclick : args.updateFilter.bind(null, item)}, item.label)
                        );
                    })
                ]),
                m('h5', 'Tags'),
                m('ul', [
                    args.tagFilters.map(function(item){
                        selectedCSS = item.id === args.activeFilter() ? '.active' : '';
                        return m('li' + selectedCSS,
                            m('a', { href : '#', onclick : args.updateFilter.bind(null, item)}, item.label)
                        );
                    })
                ])
            ]
        );
    }
};

/**
 * Information Module.
 * @constructor
 */
var Information = {
    view : function (ctrl, args) {
        var template = '';
        if (args.selected().length === 1) {
            var item = args.selected()[0];
            template = m('', [
                m('h4', m('a', { href : item.data.links.html}, item.data.attributes.title)),
                m('p.fb-info-meta.text-muted', [
                    m('span', item.data.attributes.public ? 'Public' : 'Private' + ' ' + item.data.attributes.category),
                    m('span', ', Last Modified on ' + item.data.date.local)
                ]),
                m('p', [
                    m('span', item.data.attributes.description)
                ]),
                m('p.m-t-md', [
                    m('h5', 'Tags'),
                    item.data.attributes.tags.map(function(tag){
                        return m('span.tag', tag);
                    })
                ]),
                m('p.m-t-md', [
                    m('h5', 'Jump to Page'),
                    m('a.p-xs', { href : item.data.links.html + 'wiki/home'}, 'Wiki'),
                    m('a.p-xs', { href : item.data.links.html + 'files/'}, 'Files'),
                    m('a.p-xs', { href : item.data.links.html + 'settings/'}, 'Settings'),
                ]),
                m.component(ActivityLogs, { activityLogs : args.activityLogs })
            ]);
        }
        if (args.selected().length > 1) {
            template = m('', [ '', args.selected().map(function(item){
                    return m('.fb-info-multi', [
                        m('h4', m('a', { href : item.data.links.html}, item.data.attributes.title)),
                        m('p.fb-info-meta.text-muted', [
                            m('span', item.data.attributes.public ? 'Public' : 'Private' + ' ' + item.data.attributes.category),
                            m('span', ', Last Modified on ' + item.data.date.local)
                        ]),
                    ]);
                })]);
        }
        return m('.fb-information', template);
    }
};


var ActivityLogs = {
    view : function (ctrl, args) {
        return m('.fb-activity-list.m-t-md', [
            m('h5', 'Activity Logs'),
            args.activityLogs ? args.activityLogs().map(function(item){
                return m('.fb-activity-item.osf-box.p-sm', item.date + ' ' + item.text);
            }) : ''
        ]);
    }
};

/**
 * Modals views.
 * @constructor
 */

var Modals = {
    view : function(ctrl, args) {
        return m('.fb-Modals', [
            m('#addColl.modal.fade[tabindex=-1][role="dialog"][aria-labelledby="addCollLabel"][aria-hidden="true"]',
                m('.modal-dialog',
                    m('.modal-content', [
                        m('.modal-header', [
                            m('button.close[data-dismiss="modal"][aria-label="Close"]', [
                                m('span[aria-hidden="true"]','×'),
                            ]),
                            m('h3.modal-title#addCollLabel', 'Add New Collection')
                        ]),
                        m('.modal-body', [
                            m('p', 'Collections are groups of projects that help you organize your work. [Learn more] about how to use Collections to organize your workflow. '),
                            m('.form-inline', [
                                m('.form-group', [
                                    m('label[for="addCollInput]', 'Collection Name'),
                                    m('input[type="text"].form-control#addCollInput')
                                ])
                            ]),
                            m('p', 'After you create your collection drag and drop projects to the collection. ')
                        ]),
                        m('.modal-footer', [
                            m('button[type="button"].btn.btn-default[data-dismiss="modal"]', 'Close'),
                            m('button[type="button"].btn.btn-success', 'Add')
                        ])
                    ])
                )
            ),
            m('#renameColl.modal.fade[tabindex=-1][role="dialog"][aria-labelledby="renameCollLabel"][aria-hidden="true"]',
                m('.modal-dialog',
                    m('.modal-content', [
                        m('.modal-header', [
                            m('button.close[data-dismiss="modal"][aria-label="Close"]', [
                                m('span[aria-hidden="true"]','×'),
                            ]),
                            m('h3.modal-title#renameCollLabel', 'Rename Collection')
                        ]),
                        m('.modal-body', [
                            m('p', 'Collections are groups of projects that help you organize your work. [Learn more] about how to use Collections to organize your workflow. '),
                            m('.form-inline', [
                                m('.form-group', [
                                    m('label[for="addCollInput]', 'Collection Name'),
                                    m('input[type="text"].form-control#addCollInput', { value : args.collectionMenuObject().item.label})
                                ])
                            ]),
                        ]),
                        m('.modal-footer', [
                            m('button[type="button"].btn.btn-default[data-dismiss="modal"]', 'Close'),
                            m('button[type="button"].btn.btn-success', 'Rename')
                        ])
                    ])
                )
            ),
            m('#removeColl.modal.fade[tabindex=-1][role="dialog"][aria-labelledby="removeCollLabel"][aria-hidden="true"]',
                m('.modal-dialog',
                    m('.modal-content', [
                        m('.modal-header', [
                            m('button.close[data-dismiss="modal"][aria-label="Close"]', [
                                m('span[aria-hidden="true"]','×'),
                            ]),
                            m('h3.modal-title#removeCollLabel', 'Delete Collection ' + args.collectionMenuObject().item.label)
                        ]),
                        m('.modal-body', [
                            m('p', 'You sure?'),

                        ]),
                        m('.modal-footer', [
                            m('button[type="button"].btn.btn-default[data-dismiss="modal"]', 'Close'),
                            m('button[type="button"].btn.btn-danger', 'Delete')
                        ])
                    ])
                )
            ),
            m('#infoModal.modal.fade[tabindex=-1][role="dialog"][aria-hidden="true"]',
                m('.modal-dialog',
                    m('.modal-content', [
                        m('.modal-body', [
                            m('button.close[data-dismiss="modal"][aria-label="Close"]', [
                                m('span[aria-hidden="true"]','×'),
                            ]),
                            m.component(Information, { selected : args.selected })
                        ]),
                    ])
                )
            )
        ]);
    }
};

module.exports = FileBrowser;