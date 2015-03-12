/*global describe, it, expect, example, before, after, beforeEach, afterEach, mocha, sinon*/
'use strict';
var $ = require('jquery');

var assert = require('chai').assert;

var utils = require('tests/utils');
var faker = require('faker');

var s3NodeConfigVM = require('../s3NodeConfig')._S3NodeConfigViewModel;

var API_BASE = '/api/v1/12345/s3';
var makeSettingsEndpoint = function(result, urls) {
    var settingsUrl = [API_BASE, 'settings', ''].join('/');
    return {
        method: 'GET',
        url: settingsUrl,
        response: {
            result: $.extend({}, {
                current_bucket: faker.internet.domainWord(),
                urls: $.extend({}, {
                    create_bucket: [API_BASE, 'newbucket', ''].join('/'),
                    import_auth: [API_BASE, 'import-auth', ''].join('/'),
                    create_auth: [API_BASE, 'authorize', ''].join('/'),
                    deauthorize: settingsUrl,
                    bucket_list: [API_BASE, 'buckets', ''].join('/'),
                    set_bucket: settingsUrl,
                    settings: '/settings/addons/'
                }, urls)
            }, result)
        }
    };
};

var noop = function() {};
var Case = function(cfg) {
    this.description = cfg.description || '';
    this.endpoint = cfg.endpoint || {
        response: {
            result: {}
        }
    };
    this.expected = this.endpoint.response.result;
    $.extend(this.expected, cfg.data || {});
};
Case.prototype.run = function(test) {
    var server;
    this.before = function() {
        server = utils.createServer(sinon, [this.endpoint]);
    }.bind(this);
    this.after = function() {
        server.restore();
    }.bind(this);
    test(this);
};
var Cases = function(test, cases) {
    for (var i = 0; i < cases.length; i++) {
        new Case(cases[i]).run(test);
    }
};

describe('s3NodeConfigViewModel', () => {
    new Cases(
        function(tc) {
            var expected = tc.expected;
            describe(tc.description, () => {
                before(tc.before);
                after(tc.after);
                it('fetches data from the server and updates its state', (done) => {
                    var vm = new s3NodeConfigVM('/api/v1/12345/s3/settings/', '');
                    vm.fetchFromServer(function() {
                        // VM is updated with data from the fake server
                        // observables
                        assert.equal(vm.ownerName(), expected.owner);
                        assert.equal(vm.nodeHasAuth(), expected.node_has_auth);
                        assert.equal(vm.userHasAuth(), expected.user_has_auth);
                        assert.equal(vm.currentBucket(), (expected.current_bucket === null) ? 'None' : '');
                        assert.deepEqual(vm.urls(), expected.urls);
                        done();
                    });
                });
                describe('... and after updating computed values work as expected', () => {
                    it('shows settings if Node has auth', (done) => {
                        var vm = new s3NodeConfigVM('/api/v1/12345/s3/settings/', '');
                        vm.fetchFromServer(function() {
                            assert.equal(vm.showSettings(), expected.showSettings);
                            done();
                        });
                    });
                    it('disables settings in User dosen\'t have auth and is not auth owner', (done) => {
                        var vm = new s3NodeConfigVM('/api/v1/12345/s3/settings/', '');
                        vm.fetchFromServer(function() {
                            assert.equal(vm.disableSettings(), expected.disableSettings);
                            done();
                        });
                    });
                    it('shows the new bucket button if User has auth and is auth owner', (done) => {
                        var vm = new s3NodeConfigVM('/api/v1/12345/s3/settings/', '');
                        vm.fetchFromServer(function() {
                            assert.equal(vm.showNewBucket(), expected.showNewBucket);
                            done();
                        });
                    });
                    it('shows the import auth link if User has auth and Node is unauthorized', (done) => {
                        var vm = new s3NodeConfigVM('/api/v1/12345/s3/settings/', '');
                        vm.fetchFromServer(function() {
                            assert.equal(vm.showImportAuth(), expected.showImportAuth);
                            done();
                        });
                    });
                    it('shows the create credentials link if User is unauthorized and Node is unauthorized ', (done) => {
                        var vm = new s3NodeConfigVM('/api/v1/12345/s3/settings/', '');
                        vm.fetchFromServer(function() {
                            assert.equal(vm.showCreateCredentials(), expected.showCreateCredentials);
                            done();
                        });
                    });
                });
            });
        }, [{
            description: 'when Node is unauthorized and User is unauthorized',
            endpoint: makeSettingsEndpoint({
                node_has_auth: false,
                user_has_auth: false,
                user_is_owner: false,
                owner: null,
                current_bucket: null
            }),
            data: {
                showSettings: false,
                disableSettings: true,
                showNewBucket: false,
                showImportAuth: false,
                showCreateCredentials: false
            }
        }, {
            description: 'when Node is authorized and User not auth owner',
            endpoint: makeSettingsEndpoint({
                node_has_auth: true,
                user_has_auth: false,
                user_is_owner: false,
                owner: faker.name.findName(),
                current_bucket: null
            }),
            data: {
                showSettings: true,
                disableSettings: true,
                showNewBucket: false,
                showImportAuth: false,
                showCreateCredentials: false
            }
        }, {
            description: 'when Node is unauthorized and User has auth',
            endpoint: makeSettingsEndpoint({
                node_has_auth: false,
                user_has_auth: true,
                user_is_owner: true,
                owner: faker.name.findName(),
                current_bucket: null
            }),
            data: {
                showSettings: false,
                disableSettings: false,
                showNewBucket: true,
                showImportAuth: true,
                showCreateCredentials: false
            }
        }, {
            description: 'when Node is unauthorized and User is unauthorized',
            endpoint: makeSettingsEndpoint({
                node_has_auth: false,
                user_has_auth: false,
                user_is_owner: false,
                owner: null,
                current_bucket: null
            }),
            data: {
                showSettings: false,
                disableSettings: false,
                showNewBucket: false,
                showImportAuth: false,
                showCreateCredentials: true
            }
        }]
    );
});
