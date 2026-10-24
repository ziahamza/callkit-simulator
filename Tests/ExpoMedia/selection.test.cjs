const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'Expo/plugin/withVoIPSDK.cjs'), 'utf8');
const mod = (name) => (config, callback) => {
  const value = { modResults: config[name] ?? {} };
  callback(value);
  return { ...config, [name]: value.modResults };
};
const context = {
  Buffer,
  module: { exports: {} },
  require(name) {
    if (name === 'expo/config-plugins') return {
      createRunOncePlugin: (plugin) => plugin,
      withInfoPlist: mod('info'),
      withEntitlementsPlist: mod('entitlements'),
      withPodfileProperties: mod('properties'),
      withXcodeProject: () => { throw new Error('resource mutation forbidden'); },
    };
    if (name === '../../package.json') return { version: '0.0.0' };
    if (name === 'node:fs') return fs;
    if (name === 'node:path') return path;
    throw new Error('Unexpected import: ' + name);
  },
};
const sandbox = vm.createContext(context);
vm.runInContext(source, sandbox, { filename: 'withVoIPSDK.cjs' });
const configure = (options, config = {}) => {
  context.optionsJSON = JSON.stringify(options ?? {});
  context.configJSON = JSON.stringify(config);
  return JSON.parse(JSON.stringify(vm.runInContext(
    'module.exports(JSON.parse(configJSON), JSON.parse(optionsJSON))', sandbox,
  )));
};

test('default and explicit selection bind plist and pod property before native startup', () => {
  for (const adapter of [undefined, 'webrtc', 'livekit', 'none']) {
    const result = configure(adapter === undefined ? undefined : { mediaAdapter: adapter });
    const expected = adapter ?? 'webrtc';
    assert.equal(result.info.VoIPSDK.mediaAdapter, expected);
    assert.equal(result.properties['ios-voip-sdk.mediaAdapter'], expected);
    assert.deepEqual(result.info.UIBackgroundModes, expected === 'none' ? ['voip'] : ['voip', 'audio']);
  }
});

test('selection preserves unrelated configuration and does not duplicate background modes', () => {
  const result = configure({ mediaAdapter: 'webrtc' }, {
    info: { UIBackgroundModes: ['fetch', 'audio'], VoIPSDK: { Custom: true } },
    properties: { Other: 'kept' }, entitlements: { Other: true },
  });
  assert.deepEqual(result.info.UIBackgroundModes, ['fetch', 'audio', 'voip']);
  assert.equal(result.info.VoIPSDK.Custom, true);
  assert.equal(result.properties.Other, 'kept');
  assert.equal(result.entitlements.Other, true);
});

test('unknown or malformed media selection fails instead of silently choosing a backend', () => {
  for (const mediaAdapter of ['unknown', null, true, {}]) {
    assert.throws(() => configure({ mediaAdapter }), /mediaAdapter/);
  }
});

test('example autolinking includes only the selected native WebRTC provider', () => {
  const input = fs.readFileSync(path.join(root, 'example/react-native.config.js'), 'utf8');
  for (const mediaAdapter of ['none', 'webrtc', 'livekit']) {
    const config = { module: { exports: {} }, process: { env: { SDK_EXAMPLE_MEDIA_ADAPTER: mediaAdapter } } };
    vm.runInNewContext(input, config);
    const excluded = config.module.exports.dependencies;
    assert.equal(excluded['react-native-webrtc']?.platforms.ios === null, mediaAdapter !== 'webrtc');
    for (const name of ['@livekit/react-native', '@livekit/react-native-webrtc']) {
      assert.equal(excluded[name]?.platforms.ios === null, mediaAdapter !== 'livekit');
    }
  }
});
