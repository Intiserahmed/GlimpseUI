"""
Generates a minimal Xcode project for GlimpseUIBridge.
Run once: python3 create_project.py
"""
import os, subprocess

PROJECT_NAME = "GlimpseUIBridge"
BUNDLE_ID    = "com.glimpseui.bridge"

pbxproj = f'''// !$*UTF8*$!
{{
	archiveVersion = 1;
	classes = {{}};
	objectVersion = 56;
	objects = {{
		/* Build configuration list for PBXProject */
		1A000001 /* Debug */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
				ALWAYS_SEARCH_USER_PATHS = NO;
				CLANG_ENABLE_MODULES = YES;
				CLANG_ENABLE_OBJC_ARC = YES;
				CODE_SIGN_STYLE = Automatic;
				DEVELOPMENT_TEAM = "";
				ENABLE_TESTABILITY = YES;
				IPHONEOS_DEPLOYMENT_TARGET = 16.0;
				PRODUCT_NAME = "$(TARGET_NAME)";
				SDKROOT = iphonesimulator;
				SWIFT_VERSION = 5.0;
				CODE_SIGNING_ALLOWED = NO;
			}};
			name = Debug;
		}};
		1A000002 /* Release */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
				ALWAYS_SEARCH_USER_PATHS = NO;
				CLANG_ENABLE_MODULES = YES;
				CLANG_ENABLE_OBJC_ARC = YES;
				CODE_SIGN_STYLE = Automatic;
				DEVELOPMENT_TEAM = "";
				IPHONEOS_DEPLOYMENT_TARGET = 16.0;
				PRODUCT_NAME = "$(TARGET_NAME)";
				SDKROOT = iphonesimulator;
				SWIFT_VERSION = 5.0;
				CODE_SIGNING_ALLOWED = NO;
			}};
			name = Release;
		}};
		1A000003 /* Debug */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
				BUNDLE_LOADER = "$(TEST_HOST)";
				PRODUCT_BUNDLE_IDENTIFIER = "{BUNDLE_ID}.tests";
				PRODUCT_NAME = "$(TARGET_NAME)";
				SWIFT_VERSION = 5.0;
				TEST_HOST = "";
				TESTHOST_BUNDLE_ID = "";
			}};
			name = Debug;
		}};
		1A000004 /* Release */ = {{
			isa = XCBuildConfiguration;
			buildSettings = {{
				BUNDLE_LOADER = "$(TEST_HOST)";
				PRODUCT_BUNDLE_IDENTIFIER = "{BUNDLE_ID}.tests";
				PRODUCT_NAME = "$(TARGET_NAME)";
				SWIFT_VERSION = 5.0;
				TEST_HOST = "";
				TESTHOST_BUNDLE_ID = "";
			}};
			name = Release;
		}};
		1A000010 /* Build configuration list for PBXProject */ = {{
			isa = XCConfigurationList;
			buildConfigurations = (1A000001, 1A000002);
			defaultConfigurationIsVisible = 0;
			defaultConfigurationName = Release;
		}};
		1A000011 /* Build configuration list for target */ = {{
			isa = XCConfigurationList;
			buildConfigurations = (1A000003, 1A000004);
			defaultConfigurationIsVisible = 0;
			defaultConfigurationName = Release;
		}};
		1A000020 /* {PROJECT_NAME} */ = {{
			isa = PBXFileReference;
			lastKnownFileType = wrapper.pb-project;
			name = "{PROJECT_NAME}";
			path = "{PROJECT_NAME}.xcodeproj";
			sourceTree = "<absolute>";
		}};
		1A000030 /* GlimpseUIBridgeTests.swift */ = {{
			isa = PBXFileReference;
			lastKnownFileType = sourcecode.swift;
			path = GlimpseUIBridgeTests.swift;
			sourceTree = "<group>";
			name = GlimpseUIBridgeTests.swift;
		}};
		1A000040 /* {PROJECT_NAME}.xctest */ = {{
			isa = PBXFileReference;
			explicitFileType = wrapper.cfbundle;
			includeInIndex = 0;
			path = "{PROJECT_NAME}.xctest";
			sourceTree = BUILT_PRODUCTS_DIR;
		}};
		1A000050 /* Sources */ = {{
			isa = PBXBuildFile;
			fileRef = 1A000030;
		}};
		1A000060 /* PBXGroup */ = {{
			isa = PBXGroup;
			children = (1A000030, 1A000040);
			path = GlimpseUIBridge;
			sourceTree = "<group>";
		}};
		1A000070 /* PBXSourcesBuildPhase */ = {{
			isa = PBXSourcesBuildPhase;
			buildActionMask = 2147483647;
			files = (1A000050);
			runOnlyForDeploymentPostprocessing = 0;
		}};
		1A000080 /* PBXFrameworksBuildPhase */ = {{
			isa = PBXFrameworksBuildPhase;
			buildActionMask = 2147483647;
			files = ();
			runOnlyForDeploymentPostprocessing = 0;
		}};
		1A000090 /* PBXNativeTarget */ = {{
			isa = PBXNativeTarget;
			buildConfigurationList = 1A000011;
			buildPhases = (1A000070, 1A000080);
			buildRules = ();
			dependencies = ();
			name = "{PROJECT_NAME}";
			productName = "{PROJECT_NAME}";
			productReference = 1A000040;
			productType = "com.apple.product-type.bundle.ui-testing";
		}};
		1A0000A0 /* PBXProject */ = {{
			isa = PBXProject;
			buildConfigurationList = 1A000010;
			compatibilityVersion = "Xcode 14.0";
			developmentRegion = en;
			hasScannedForEncodings = 0;
			knownRegions = (en, Base);
			mainGroup = 1A000060;
			productRefGroup = 1A000060;
			projectDirPath = "";
			projectRoot = "";
			targets = (1A000090);
		}};
	}};
	rootObject = 1A0000A0;
}}
'''

os.makedirs(f"{PROJECT_NAME}.xcodeproj", exist_ok=True)
with open(f"{PROJECT_NAME}.xcodeproj/project.pbxproj", "w") as f:
    f.write(pbxproj)

os.makedirs(f"{PROJECT_NAME}", exist_ok=True)

# Move swift file into correct location
swift_src = f"{PROJECT_NAME}/{PROJECT_NAME}Tests.swift"
if not os.path.exists(swift_src):
    import shutil
    shutil.copy(f"{PROJECT_NAME}/{PROJECT_NAME}Tests.swift",
                f"{PROJECT_NAME}/{PROJECT_NAME}Tests.swift")

print(f"✅ Xcode project created: {PROJECT_NAME}.xcodeproj")
print(f"   Run: xcodebuild test -project {PROJECT_NAME}.xcodeproj -scheme {PROJECT_NAME} -destination 'platform=iOS Simulator,name=iPhone 17 Pro'")
