#!/usr/bin/env node
// Host-only: evaluates real Expo mods and Xcode project helpers without prebuild.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const xcode = require('xcode');
const plugin = require('../app.plugin.js');

const checks = [];
function configured(options) {
  return plugin({ name: 'Fixture', slug: 'fixture', ios: {} }, options);
}
async function apply(config, mod, results, root) {
  return (
    await config.mods.ios[mod]({
      ...config,
      modResults: results,
      modRawConfig: {},
      modRequest: {
        projectRoot: root,
        platformProjectRoot: root && path.join(root, 'ios'),
        projectName: 'Fixture',
      },
    })
  ).modResults;
}
function fixtureProject() {
  const project = xcode.project('host-fixture.pbxproj');
  project.hash = {
    project: {
      rootObject: 'PROJECT',
      objects: {
        PBXBuildFile: {},
        PBXFileReference: {},
        PBXGroup: {
          MAIN: {
            isa: 'PBXGroup',
            children: [{ value: 'APP', comment: 'Fixture' }],
            sourceTree: '"<group>"',
          },
          APP: {
            isa: 'PBXGroup',
            name: 'Fixture',
            path: 'Fixture',
            children: [],
            sourceTree: '"<group>"',
          },
        },
        PBXProject: {
          PROJECT: {
            isa: 'PBXProject',
            mainGroup: 'MAIN',
            targets: [{ value: 'TARGET', comment: 'Fixture' }],
          },
        },
        PBXNativeTarget: {
          TARGET: {
            isa: 'PBXNativeTarget',
            name: 'Fixture',
            productType: '"com.apple.product-type.application"',
            buildPhases: [{ value: 'RESOURCES', comment: 'Resources' }],
          },
        },
        PBXResourcesBuildPhase: {
          RESOURCES: {
            isa: 'PBXResourcesBuildPhase',
            files: [],
            buildActionMask: 2147483647,
            runOnlyForDeploymentPostprocessing: 0,
          },
          RESOURCES_comment: 'Resources',
        },
      },
    },
  };
  return project;
}
async function rejectsMod(options, mod, results, expected, root) {
  await assert.rejects(
    () => apply(configured(options), mod, results, root),
    expected,
  );
}

async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ios-voip-plugin-'));
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), 'ios-voip-plugin-outside-'),
  );
  try {
    const defaults = configured();
    assert.deepEqual(Object.keys(defaults.mods), ['ios']);
    assert.deepEqual(Object.keys(defaults.mods.ios).sort(), [
      'entitlements',
      'infoPlist',
      'podfileProperties',
    ]);
    checks.push('apple-mods-only');
    const plist = await apply(defaults, 'infoPlist', {
      UIBackgroundModes: ['audio', 'location'],
      Custom: { nested: true },
      VoIPSDK: { custom: 'preserved', iconTemplateImageName: 'old.png' },
      NSUserActivityTypes: ['OtherIntent'],
    });
    assert.deepEqual(plist.UIBackgroundModes, ['audio', 'location', 'voip']);
    assert.deepEqual(plist.VoIPSDK, {
      custom: 'preserved',
      mediaAdapter: 'webrtc',
      unhandledAction: 'fail',
      simulatorSupport: true,
      includesCallsInRecents: true,
      supportsVideo: true,
      maximumConversationGroups: 2,
      maximumConversationsPerConversationGroup: 2,
      ringtoneName: 'default',
    });
    assert.deepEqual(plist.Custom, { nested: true });
    assert.deepEqual(plist.NSUserActivityTypes, ['OtherIntent']);
    checks.push('defaults-and-existing-plist-preserved');
    assert.equal(plist.VoIPSDK.providerOwnershipMonitoring, undefined);
    assert.deepEqual(
      await apply(defaults, 'podfileProperties', { custom: 'preserved' }),
      {
        custom: 'preserved',
        'ios-voip-sdk.providerOwnershipMonitoring': 'false',
        'ios-voip-sdk.mediaAdapter': 'webrtc',
      },
    );
    for (const enabled of [false, true]) {
      const monitored = configured({ providerOwnershipMonitoring: enabled });
      assert.equal(
        (await apply(monitored, 'infoPlist', {})).VoIPSDK
          .providerOwnershipMonitoring,
        enabled,
      );
      assert.equal(
        (await apply(monitored, 'podfileProperties', {}))[
          'ios-voip-sdk.providerOwnershipMonitoring'
        ],
        String(enabled),
      );
    }
    assert.throws(
      () => configured({ providerOwnershipMonitoring: 'true' }),
      /boolean/,
    );
    assert.equal(
      (
        await apply(defaults, 'infoPlist', {
          VoIPSDK: { providerOwnershipMonitoring: true },
        })
      ).VoIPSDK.providerOwnershipMonitoring,
      undefined,
    );
    checks.push(
      'provider-monitor-explicit-build-and-runtime-opt-in-with-debug-default',
    );

    assert.deepEqual(
      await apply(configured(), 'infoPlist', structuredClone(plist)),
      plist,
    );
    assert.deepEqual(
      await apply(
        configured({ simulatorSupport: undefined, sounds: undefined }),
        'infoPlist',
        structuredClone(plist),
      ),
      plist,
    );
    const originalMod = defaults.mods.ios.infoPlist;
    plugin(defaults);
    assert.equal(defaults.mods.ios.infoPlist, originalMod);
    checks.push('repeated-plugin-and-plist-idempotent');
    const custom = await apply(
      configured({
        unhandledAction: 'accept',
        simulatorSupport: false,
        includesCallsInRecents: false,
        supportsVideo: false,
        maximumConversationGroups: 3,
        maximumConversationsPerConversationGroup: 4,
        microphonePermission: 'Use microphone for calls',
        cameraPermission: false,
      }),
      'infoPlist',
      { NSCameraUsageDescription: 'Existing camera policy' },
    );
    assert.equal(custom.VoIPSDK.unhandledAction, 'accept');
    assert.equal(custom.VoIPSDK.simulatorSupport, false);
    assert.equal(custom.VoIPSDK.includesCallsInRecents, false);
    assert.equal(custom.VoIPSDK.supportsVideo, false);
    assert.equal(custom.VoIPSDK.maximumConversationGroups, 3);
    assert.equal(custom.VoIPSDK.maximumConversationsPerConversationGroup, 4);
    assert.equal(
      custom.NSMicrophoneUsageDescription,
      'Use microphone for calls',
    );
    assert.equal(custom.NSCameraUsageDescription, 'Existing camera policy');
    checks.push('explicit-native-options-and-permissions');
    assert.deepEqual(await apply(configured(), 'entitlements', {}), {
      'aps-environment': 'development',
    });
    const existing = {
      'aps-environment': 'production',
      'com.apple.security.application-groups': ['group.existing'],
    };
    assert.deepEqual(
      await apply(configured(), 'entitlements', structuredClone(existing)),
      existing,
    );
    assert.deepEqual(
      await apply(
        configured({ apsEnvironment: 'production' }),
        'entitlements',
        { 'aps-environment': 'development', custom: true },
      ),
      { 'aps-environment': 'production', custom: true },
    );
    checks.push('apns-development-default-preservation-and-explicit-selection');

    for (const options of [
      null,
      [],
      new Date(),
      { unhandledAction: 'ignore' },
      { simulatorSupport: 'true' },
      { includesCallsInRecents: 1 },
      { supportsVideo: null },
      { maximumConversationGroups: 0 },
      { maximumConversationsPerConversationGroup: 1.5 },
      { maximumConversationGroups: Number.MAX_SAFE_INTEGER + 1 },
      { apsEnvironment: 'sandbox' },
      { sounds: [1] },
      { sounds: ['a.wav', 'A.wav'] },
      { sounds: ['tone.wav'], iconTemplateIos: 'other/tone.wav' },
      { defaultRingtoneIos: 'missing.wav' },
      { sounds: ['/outside.wav'] },
      { iconTemplateIos: false },
      { microphonePermission: '' },
      { defaultCallingApp: true },
      { incomingCallTimeout: 45 },
      { typo: true },
    ]) {
      assert.throws(() => configured(options), /ios-voip-sdk:/);
    }
    checks.push('invalid-and-unimplemented-options-rejected');
    await rejectsMod(
      {},
      'infoPlist',
      { UIBackgroundModes: 'audio' },
      /UIBackgroundModes/,
    );
    await rejectsMod(
      {},
      'infoPlist',
      { UIBackgroundModes: [1] },
      /UIBackgroundModes/,
    );
    await rejectsMod({}, 'infoPlist', { VoIPSDK: [] }, /dictionary/);
    checks.push('malformed-existing-plist-rejected');

    fs.mkdirSync(path.join(root, 'assets'));
    fs.mkdirSync(path.join(root, 'ios', 'Fixture'), { recursive: true });
    fs.writeFileSync(
      path.join(root, 'assets', 'ring.wav'),
      'owned ringtone bytes',
    );
    fs.writeFileSync(
      path.join(root, 'assets', 'glyph.png'),
      Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 0]),
    );
    const resourceOptions = {
      sounds: ['assets/ring.wav'],
      defaultRingtoneIos: 'ring.wav',
      iconTemplateIos: 'assets/glyph.png',
    };
    const resourceConfig = configured(resourceOptions);
    const resourcePlist = await apply(resourceConfig, 'infoPlist', {});
    assert.equal(resourcePlist.VoIPSDK.ringtoneName, 'ring.wav');
    assert.equal(resourcePlist.VoIPSDK.iconTemplateImageName, 'glyph.png');
    const project = await apply(
      resourceConfig,
      'xcodeproj',
      fixtureProject(),
      root,
    );
    const destination = path.join(root, 'ios', 'Fixture', 'VoIPSDKResources');
    assert.equal(
      fs.readFileSync(path.join(destination, 'ring.wav'), 'utf8'),
      'owned ringtone bytes',
    );
    assert.deepEqual(
      fs.readFileSync(path.join(destination, 'glyph.png')),
      fs.readFileSync(path.join(root, 'assets', 'glyph.png')),
    );
    assert.equal(
      project.hash.project.objects.PBXResourcesBuildPhase.RESOURCES.files
        .length,
      2,
    );
    for (const name of ['ring.wav', 'glyph.png']) {
      const reference = project.hasFile(`VoIPSDKResources/${name}`);
      assert.ok(reference);
      assert.equal(reference.sourceTree, '"<group>"');
      const sourcePath = path.resolve(
        root,
        'ios',
        project.hash.project.objects.PBXGroup.APP.path,
        reference.path.replace(/^"|"$/g, ''),
      );
      assert.equal(
        fs.realpathSync(sourcePath),
        fs.realpathSync(path.join(destination, name)),
      );
    }
    checks.push('resource-bytes-resolved-xcode-paths-and-real-resource-phase');
    const before = JSON.stringify(project.hash);
    await apply(configured(resourceOptions), 'xcodeproj', project, root);
    assert.equal(JSON.stringify(project.hash), before);
    checks.push('resource-project-idempotence');
    fs.writeFileSync(
      path.join(root, 'assets', 'ring.wav'),
      'updated owned ringtone',
    );
    await apply(configured(resourceOptions), 'xcodeproj', project, root);
    assert.equal(
      fs.readFileSync(path.join(destination, 'ring.wav'), 'utf8'),
      'updated owned ringtone',
    );
    assert.equal(JSON.stringify(project.hash), before);
    checks.push('owned-resource-update-without-duplicate-project-entries');

    const collision = fixtureProject();
    collision.hash.project.objects.PBXFileReference.FOREIGN = {
      isa: 'PBXFileReference',
      path: 'Other/ring.wav',
    };
    await rejectsMod(resourceOptions, 'xcodeproj', collision, /collides/, root);
    checks.push('unrelated-xcode-filename-collision-rejected');
    await rejectsMod(
      { sounds: ['assets/missing.wav'] },
      'xcodeproj',
      fixtureProject(),
      /does not exist/,
      root,
    );
    fs.writeFileSync(path.join(root, 'assets', 'bad.png'), 'not PNG');
    await rejectsMod(
      { iconTemplateIos: 'assets/bad.png' },
      'xcodeproj',
      fixtureProject(),
      /PNG image/,
      root,
    );
    checks.push('missing-resources-and-invalid-png-rejected');
    fs.writeFileSync(
      path.join(outside, 'external.wav'),
      'private outside fixture',
    );
    await rejectsMod(
      { sounds: [path.relative(root, path.join(outside, 'external.wav'))] },
      'xcodeproj',
      fixtureProject(),
      /escapes/,
      root,
    );
    fs.symlinkSync(
      path.join(outside, 'external.wav'),
      path.join(root, 'assets', 'link.wav'),
    );
    await rejectsMod(
      { sounds: ['assets/link.wav'] },
      'xcodeproj',
      fixtureProject(),
      /symlink escapes/,
      root,
    );
    checks.push('source-path-and-symlink-boundaries');
    fs.unlinkSync(path.join(destination, 'ring.wav'));
    fs.symlinkSync(
      path.join(outside, 'external.wav'),
      path.join(destination, 'ring.wav'),
    );
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      project,
      /ordinary owned file/,
      root,
    );
    assert.equal(
      fs.readFileSync(path.join(outside, 'external.wav'), 'utf8'),
      'private outside fixture',
    );
    checks.push('destination-symlink-does-not-overwrite-unrelated-file');
    fs.unlinkSync(path.join(destination, 'ring.wav'));
    fs.symlinkSync(
      path.join(outside, 'new.wav'),
      path.join(destination, 'ring.wav'),
    );
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      project,
      /ordinary owned file/,
      root,
    );
    assert.equal(fs.existsSync(path.join(outside, 'new.wav')), false);
    checks.push('dangling-destination-symlink-does-not-create-external-file');
    fs.unlinkSync(path.join(destination, 'ring.wav'));
    fs.linkSync(
      path.join(outside, 'external.wav'),
      path.join(destination, 'ring.wav'),
    );
    await apply(configured(resourceOptions), 'xcodeproj', project, root);
    assert.equal(
      fs.readFileSync(path.join(outside, 'external.wav'), 'utf8'),
      'private outside fixture',
    );
    assert.equal(
      fs.readFileSync(path.join(destination, 'ring.wav'), 'utf8'),
      'updated owned ringtone',
    );
    checks.push('atomic-resource-update-preserves-hardlink-referent');
    fs.renameSync(destination, `${destination}-saved`);
    const unrelated = path.join(root, 'unrelated');
    fs.mkdirSync(unrelated);
    fs.writeFileSync(
      path.join(unrelated, 'ring.wav'),
      'unrelated in-project file',
    );
    fs.symlinkSync(unrelated, destination);
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      project,
      /destination escapes/,
      root,
    );
    assert.equal(
      fs.readFileSync(path.join(unrelated, 'ring.wav'), 'utf8'),
      'unrelated in-project file',
    );
    fs.unlinkSync(destination);
    fs.renameSync(`${destination}-saved`, destination);
    checks.push('reserved-directory-symlink-does-not-overwrite-project-file');
    fs.renameSync(path.join(root, 'ios'), path.join(root, 'native-ios'));
    fs.symlinkSync(path.join(root, 'native-ios'), path.join(root, 'ios'));
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      project,
      /destination escapes/,
      root,
    );
    fs.unlinkSync(path.join(root, 'ios'));
    fs.renameSync(path.join(root, 'native-ios'), path.join(root, 'ios'));
    checks.push('platform-directory-symlink-rejected-before-canonicalization');
    const orphan = fixtureProject();
    orphan.hash.project.objects.PBXFileReference.ORPHAN = {
      isa: 'PBXFileReference',
      path: 'VoIPSDKResources/ring.wav',
      sourceTree: '"<group>"',
    };
    orphan.hash.project.objects.PBXGroup.APP.children.push({
      value: 'ORPHAN',
      comment: 'ring.wav',
    });
    await rejectsMod(resourceOptions, 'xcodeproj', orphan, /not bound/, root);
    checks.push('existing-reference-without-resource-phase-rejected');
    const wrongLayout = fixtureProject();
    wrongLayout.hash.project.objects.PBXGroup.APP.path = 'Different';
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      wrongLayout,
      /unsupported layout/,
      root,
    );
    checks.push('unsupported-project-group-layout-rejected');
    const wrongRoot = fixtureProject();
    wrongRoot.hash.project.objects.PBXGroup.MAIN.path = 'Sources';
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      wrongRoot,
      /unsupported layout/,
      root,
    );
    delete wrongRoot.hash.project.objects.PBXGroup.MAIN.path;
    wrongRoot.hash.project.objects.PBXGroup.MAIN.sourceTree = 'SOURCE_ROOT';
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      wrongRoot,
      /unsupported layout/,
      root,
    );
    checks.push('unsupported-root-group-base-layout-rejected');
    const childCollision = fixtureProject();
    childCollision.hash.project.objects.PBXGroup.OTHER = {
      isa: 'PBXGroup',
      path: 'Other',
      children: [],
      sourceTree: '"<group>"',
    };
    childCollision.hash.project.objects.PBXGroup.APP.children.push({
      value: 'OTHER',
      comment: 'ring.wav',
    });
    await rejectsMod(
      resourceOptions,
      'xcodeproj',
      childCollision,
      /group child/,
      root,
    );
    checks.push('helper-child-name-collision-rejected');
    const sources = [
      'app.plugin.js',
      'Expo/plugin/withVoIPSDK.cjs',
      'discovery/check_expo_plugin.cjs',
    ];
    console.log(
      JSON.stringify(
        {
          success: true,
          classification:
            'measured host Expo mod controls; no native build or registration',
          count: checks.length,
          checks,
          expoVersion: require('expo/package.json').version,
          sourceFiles: Object.fromEntries(
            sources.map((file) => [
              file,
              crypto
                .createHash('sha256')
                .update(fs.readFileSync(path.join(__dirname, '..', file)))
                .digest('hex'),
            ]),
          ),
        },
        null,
        2,
      ),
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
