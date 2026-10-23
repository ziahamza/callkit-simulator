const {
  createRunOncePlugin,
  IOSConfig,
  withEntitlementsPlist,
  withInfoPlist,
  withPodfileProperties,
  withXcodeProject,
} = require('expo/config-plugins');
const fs = require('node:fs');
const path = require('node:path');

const PREFIX = 'ios-voip-sdk: ';
const RESOURCE_DIRECTORY = 'VoIPSDKResources';
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const DEFAULTS = {
  mediaAdapter: 'webrtc',
  unhandledAction: 'fail',
  simulatorSupport: true,
  defaultCallingApp: false,
  includesCallsInRecents: true,
  supportsVideo: true,
  maximumConversationGroups: 2,
  maximumConversationsPerConversationGroup: 2,
  defaultRingtoneIos: 'default',
  sounds: [],
};
const KEYS = new Set([
  ...Object.keys(DEFAULTS),
  'apsEnvironment',
  'iconTemplateIos',
  'microphonePermission',
  'cameraPermission',
  'providerOwnershipMonitoring',
]);

function fail(message) {
  throw new Error(PREFIX + message);
}

function isDictionary(value) {
  if (value === null || typeof value !== 'object') return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function validate(options) {
  if (options === undefined) options = {};
  if (!isDictionary(options)) fail('plugin options must be an object.');
  for (const key of Object.keys(options)) {
    if (!KEYS.has(key)) fail(`unsupported option "${key}".`);
  }
  const settings = {
    ...DEFAULTS,
    ...Object.fromEntries(
      Object.entries(options).filter(([, value]) => value !== undefined),
    ),
  };
  if (
    settings.providerOwnershipMonitoring !== undefined &&
    typeof settings.providerOwnershipMonitoring !== 'boolean'
  )
    fail('providerOwnershipMonitoring must be a boolean.');
  if (!['webrtc', 'livekit', 'none'].includes(settings.mediaAdapter))
    fail('mediaAdapter must be webrtc, livekit, or none.');
  if (!['fail', 'accept'].includes(settings.unhandledAction))
    fail('unhandledAction must be fail or accept.');
  for (const key of [
    'simulatorSupport',
    'defaultCallingApp',
    'includesCallsInRecents',
    'supportsVideo',
  ])
    if (typeof settings[key] !== 'boolean') fail(`${key} must be a boolean.`);
  for (const key of [
    'maximumConversationGroups',
    'maximumConversationsPerConversationGroup',
  ])
    if (!Number.isSafeInteger(settings[key]) || settings[key] < 1)
      fail(`${key} must be a positive integer.`);
  if (
    settings.apsEnvironment !== undefined &&
    !['development', 'production'].includes(settings.apsEnvironment)
  )
    fail('apsEnvironment must be development or production.');
  for (const key of ['microphonePermission', 'cameraPermission']) {
    if (
      settings[key] !== undefined &&
      settings[key] !== false &&
      (typeof settings[key] !== 'string' || !settings[key].trim())
    )
      fail(`${key} must be a nonempty string or false.`);
  }
  if (
    !Array.isArray(settings.sounds) ||
    settings.sounds.some(
      (file) => typeof file !== 'string' || !file.trim() || file.includes('\0'),
    )
  )
    fail('sounds must be an array of nonempty relative file paths.');
  if (
    settings.iconTemplateIos !== undefined &&
    (typeof settings.iconTemplateIos !== 'string' ||
      !settings.iconTemplateIos.trim() ||
      settings.iconTemplateIos.includes('\0'))
  )
    fail('iconTemplateIos must be a relative PNG path.');
  const files = [
    ...settings.sounds,
    ...(settings.iconTemplateIos === undefined
      ? []
      : [settings.iconTemplateIos]),
  ];
  const filenames = new Set();
  for (const file of files) {
    if (path.isAbsolute(file))
      fail('resource paths must be relative to the Expo project.');
    const name = path.basename(file).toLowerCase();
    if (!name || name === '.' || name === '..' || filenames.has(name))
      fail('resource filenames must be unique, including case.');
    filenames.add(name);
  }
  if (
    typeof settings.defaultRingtoneIos !== 'string' ||
    (settings.defaultRingtoneIos !== 'default' &&
      !settings.sounds.some(
        (file) => path.basename(file) === settings.defaultRingtoneIos,
      ))
  )
    fail(
      'defaultRingtoneIos must be default or the filename of a provided sound.',
    );
  return settings;
}

function inside(root, candidate) {
  const relative = path.relative(root, candidate);
  return (
    relative !== '..' &&
    !relative.startsWith(`..${path.sep}`) &&
    !path.isAbsolute(relative)
  );
}

function resourceSources(projectRoot, settings) {
  const root = fs.realpathSync(projectRoot);
  return [
    ...settings.sounds,
    ...(settings.iconTemplateIos === undefined
      ? []
      : [settings.iconTemplateIos]),
  ].map((file) => {
    const candidate = path.resolve(root, file);
    if (!inside(root, candidate)) fail(`resource escapes the project: ${file}`);
    if (!fs.existsSync(candidate) || !fs.statSync(candidate).isFile())
      fail(`resource file does not exist: ${file}`);
    const source = fs.realpathSync(candidate);
    if (!inside(root, source))
      fail(`resource symlink escapes the project: ${file}`);
    if (
      file === settings.iconTemplateIos &&
      (path.extname(file).toLowerCase() !== '.png' ||
        !fs.readFileSync(source).subarray(0, 8).equals(PNG_SIGNATURE))
    )
      fail('iconTemplateIos must contain a PNG image.');
    return { source, filename: path.basename(file) };
  });
}

function withResources(config, settings) {
  if (!settings.sounds.length && settings.iconTemplateIos === undefined)
    return config;
  return withXcodeProject(config, (mod) => {
    const { projectRoot, projectName } = mod.modRequest;
    if (
      typeof projectName !== 'string' ||
      !projectName ||
      path.basename(projectName) !== projectName ||
      ['.', '..'].includes(projectName)
    )
      fail('the iOS project name is missing or invalid.');
    const root = fs.realpathSync(projectRoot);
    const requestedRoot = path.resolve(projectRoot);
    const requestedPlatform = path.resolve(
      mod.modRequest.platformProjectRoot || path.join(requestedRoot, 'ios'),
    );
    // Canonicalize the trusted project root without erasing symlinks below it.
    const platformRoot = inside(requestedRoot, requestedPlatform)
      ? path.resolve(root, path.relative(requestedRoot, requestedPlatform))
      : requestedPlatform;
    const projectDirectory = path.join(platformRoot, projectName);
    const destination = path.join(projectDirectory, RESOURCE_DIRECTORY);
    if (!inside(root, destination))
      fail('the iOS resource destination escapes the Expo project.');
    let directory = root;
    for (const component of path.relative(root, destination).split(path.sep)) {
      directory = path.join(directory, component);
      const entry = fs.lstatSync(directory, { throwIfNoEntry: false });
      if (entry && (!entry.isDirectory() || entry.isSymbolicLink()))
        fail('the iOS resource destination escapes the Expo project.');
    }
    const resources = resourceSources(root, settings);
    const target = IOSConfig.XcodeUtils.getApplicationNativeTarget({
      project: mod.modResults,
      projectName,
    });
    const project = mod.modResults;
    const mainGroup = project.getPBXGroupByKey(
      project.getFirstProject().firstProject.mainGroup,
    );
    const appGroupEntry = mainGroup.children.find(
      (child) => child.comment === projectName,
    );
    const appGroup =
      appGroupEntry && project.getPBXGroupByKey(appGroupEntry.value);
    const unquote = (value) =>
      typeof value === 'string' ? value.replace(/^"|"$/g, '') : '';
    const phase = project.pbxResourcesBuildPhaseObj(target.uuid);
    if (
      !appGroup ||
      unquote(mainGroup.path) !== '' ||
      unquote(mainGroup.sourceTree) !== '<group>' ||
      unquote(appGroup.path) !== projectName ||
      unquote(appGroup.sourceTree) !== '<group>' ||
      !phase
    )
      fail(
        'the application resource group or build phase has an unsupported layout.',
      );
    // A flat app bundle cannot contain two resources with the same filename.
    const references = Object.entries(project.pbxFileReferenceSection()).filter(
      ([, reference]) => isDictionary(reference),
    );
    for (const resource of resources) {
      const filepath = path.posix.join(RESOURCE_DIRECTORY, resource.filename);
      let registeredID;
      for (const [referenceID, reference] of references) {
        const existing = unquote(reference.path);
        if (
          path.basename(existing).toLowerCase() ===
            resource.filename.toLowerCase() &&
          existing !== filepath
        )
          fail(
            `resource filename collides with an existing Xcode file: ${resource.filename}`,
          );
        if (existing === filepath) {
          const inGroup = appGroup.children.some(
            (child) => child.value === referenceID,
          );
          const inPhase = phase.files.some(
            (entry) =>
              project.pbxBuildFileSection()[entry.value]?.fileRef ===
              referenceID,
          );
          if (
            !inGroup ||
            !inPhase ||
            unquote(reference.sourceTree) !== '<group>'
          )
            fail(
              `existing resource reference is not bound to the application resource phase: ${resource.filename}`,
            );
          resource.registered = true;
          registeredID = referenceID;
        }
      }
      if (
        appGroup.children.some(
          (child) =>
            child.comment === resource.filename && child.value !== registeredID,
        )
      )
        fail(
          `resource filename collides with an existing Xcode group child: ${resource.filename}`,
        );
    }
    for (const resource of resources) {
      const target = path.join(destination, resource.filename);
      const entry = fs.lstatSync(target, { throwIfNoEntry: false });
      if (entry && (!entry.isFile() || entry.isSymbolicLink()))
        fail(
          `resource destination is not an ordinary owned file: ${resource.filename}`,
        );
    }
    fs.mkdirSync(destination, { recursive: true });
    const staging = fs.mkdtempSync(path.join(destination, '.voip-sdk-'));
    try {
      for (const resource of resources) {
        // Replacing the owned entry preserves unrelated hardlink referents.
        const temporary = path.join(staging, resource.filename);
        fs.copyFileSync(resource.source, temporary, fs.constants.COPYFILE_EXCL);
        fs.renameSync(temporary, path.join(destination, resource.filename));
      }
    } finally {
      fs.rmSync(staging, { recursive: true, force: true });
    }
    for (const resource of resources) {
      const filepath = path.posix.join(RESOURCE_DIRECTORY, resource.filename);
      if (!resource.registered) {
        mod.modResults = IOSConfig.XcodeUtils.addResourceFileToGroup({
          filepath,
          groupName: projectName,
          isBuildFile: true,
          project: mod.modResults,
        });
      }
    }
    return mod;
  });
}

function withVoIPSDK(config, options) {
  const settings = validate(options);
  config = withInfoPlist(config, (mod) => {
    const plist = mod.modResults;
    const modes =
      plist.UIBackgroundModes === undefined ? [] : plist.UIBackgroundModes;
    if (!Array.isArray(modes) || modes.some((mode) => typeof mode !== 'string'))
      fail('UIBackgroundModes must be an array of strings.');
    if (plist.VoIPSDK !== undefined && !isDictionary(plist.VoIPSDK))
      fail('Info.plist VoIPSDK must be a dictionary.');
    const activities = plist.NSUserActivityTypes ?? [];
    if (
      !Array.isArray(activities) ||
      activities.some((type) => typeof type !== 'string')
    )
      fail('NSUserActivityTypes must be an array of strings.');
    plist.NSUserActivityTypes = activities.includes('INStartCallIntent')
      ? [...activities]
      : [...activities, 'INStartCallIntent'];
    const backgroundModes = [...modes];
    for (const mode of [
      'voip',
      ...(settings.mediaAdapter === 'none' ? [] : ['audio']),
    ]) {
      if (!backgroundModes.includes(mode)) backgroundModes.push(mode);
    }
    plist.UIBackgroundModes = backgroundModes;
    plist.VoIPSDK = {
      ...plist.VoIPSDK,
      mediaAdapter: settings.mediaAdapter,
      unhandledAction: settings.unhandledAction,
      simulatorSupport: settings.simulatorSupport,
      includesCallsInRecents: settings.includesCallsInRecents,
      supportsVideo: settings.supportsVideo,
      maximumConversationGroups: settings.maximumConversationGroups,
      maximumConversationsPerConversationGroup:
        settings.maximumConversationsPerConversationGroup,
      ringtoneName: settings.defaultRingtoneIos,
    };
    if (settings.providerOwnershipMonitoring === undefined)
      delete plist.VoIPSDK.providerOwnershipMonitoring;
    else
      plist.VoIPSDK.providerOwnershipMonitoring =
        settings.providerOwnershipMonitoring;
    if (settings.iconTemplateIos === undefined)
      delete plist.VoIPSDK.iconTemplateImageName;
    else
      plist.VoIPSDK.iconTemplateImageName = path.basename(
        settings.iconTemplateIos,
      );
    for (const [option, key] of [
      ['microphonePermission', 'NSMicrophoneUsageDescription'],
      ['cameraPermission', 'NSCameraUsageDescription'],
    ]) {
      if (typeof settings[option] === 'string') plist[key] = settings[option];
    }
    return mod;
  });
  config = withPodfileProperties(config, (mod) => {
    mod.modResults['ios-voip-sdk.providerOwnershipMonitoring'] = String(
      settings.providerOwnershipMonitoring === true,
    );
    return mod;
  });
  config = withEntitlementsPlist(config, (mod) => {
    if (settings.defaultCallingApp)
      mod.modResults['com.apple.developer.calling-app'] = true;
    else delete mod.modResults['com.apple.developer.calling-app'];
    if (settings.apsEnvironment !== undefined)
      mod.modResults['aps-environment'] = settings.apsEnvironment;
    else if (mod.modResults['aps-environment'] === undefined)
      mod.modResults['aps-environment'] = 'development';
    return mod;
  });
  config = withPodfileProperties(config, (mod) => {
    mod.modResults['ios-voip-sdk.mediaAdapter'] = settings.mediaAdapter;
    return mod;
  });
  return withResources(config, settings);
}

module.exports = createRunOncePlugin(
  withVoIPSDK,
  'ios-voip-sdk',
  require('../../package.json').version,
);
