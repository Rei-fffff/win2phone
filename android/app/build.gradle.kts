plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.win2phone"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.example.win2phone"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    // M0 uses only platform APIs (android.app.Activity, MediaCodec, SurfaceView).
    // No AndroidX dependency -> faster first Gradle sync.
}
