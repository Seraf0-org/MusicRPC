# MusicRPC Bridge (Android) のビルド。Gradle を使わず SDK のツールだけで APK を作る。
#   .\build.ps1                        SDK / JDK を自動で探す
#   .\build.ps1 -Sdk <path> -Jdk <path>
# 出力: build\MusicRPC-Bridge.apk

param(
    [string]$Sdk,
    [string]$Jdk
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$api = 'android-34'

function Find-Sdk {
    $candidates = @($Sdk, $env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, "$env:LOCALAPPDATA\Android\Sdk")
    $candidates += Get-ChildItem 'C:\Program Files\Unity\Hub\Editor\*\Editor\Data\PlaybackEngines\AndroidPlayer\SDK' -Directory -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object FullName
    foreach ($c in $candidates) {
        if ($c -and (Test-Path "$c\platforms\$api\android.jar") -and (Test-Path "$c\build-tools")) { return $c }
    }
    throw "Android SDK ($api と build-tools 入り) が見つかりません。-Sdk で指定してください。"
}

function Find-Jdk([string]$sdkPath) {
    $candidates = @($Jdk, $env:JAVA_HOME, (Join-Path (Split-Path $sdkPath) 'OpenJDK'))
    foreach ($c in $candidates) {
        if ($c -and (Test-Path "$c\bin\javac.exe")) { return $c }
    }
    $javac = Get-Command javac -ErrorAction SilentlyContinue
    if ($javac) { return Split-Path (Split-Path $javac.Source) }
    throw "JDK (javac) が見つかりません。-Jdk で指定してください。"
}

function Invoke-Tool([string]$exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($exe)) が失敗しました (exit $LASTEXITCODE)" }
}

$sdkPath = Find-Sdk
$jdkPath = Find-Jdk $sdkPath
$bt = Get-ChildItem "$sdkPath\build-tools" -Directory | Sort-Object { [version]($_.Name -replace '[^\d.]', '') } | Select-Object -Last 1 -ExpandProperty FullName
$jar = "$sdkPath\platforms\$api\android.jar"
$env:JAVA_HOME = $jdkPath
$env:PATH = "$jdkPath\bin;$env:PATH"
Write-Host "SDK: $sdkPath"
Write-Host "JDK: $jdkPath"
Write-Host "build-tools: $bt"

$build = Join-Path $root 'build'
if (Test-Path $build) { Remove-Item $build -Recurse -Force }
New-Item -ItemType Directory -Path "$build\classes", "$build\dex" | Out-Null

# 1. リソースとマニフェスト
Invoke-Tool "$bt\aapt2.exe" compile --dir "$root\res" -o "$build\res.zip"
Invoke-Tool "$bt\aapt2.exe" link -o "$build\unsigned.apk" -I $jar --manifest "$root\AndroidManifest.xml" "$build\res.zip"

# 2. Java → class → dex
$sources = Get-ChildItem "$root\src" -Recurse -Filter *.java | ForEach-Object FullName
Invoke-Tool "$jdkPath\bin\javac.exe" -source 8 -target 8 -bootclasspath $jar -classpath "$bt\core-lambda-stubs.jar" `
    -encoding UTF-8 '-Xlint:-options' -d "$build\classes" @sources
$classes = Get-ChildItem "$build\classes" -Recurse -Filter *.class | ForEach-Object FullName
Invoke-Tool "$bt\d8.bat" --release --lib $jar --min-api 26 --output "$build\dex" @classes

# 3. dex を APK に入れて整列
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::Open("$build\unsigned.apk", 'Update')
try {
    [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$build\dex\classes.dex", 'classes.dex') | Out-Null
} finally {
    $zip.Dispose()
}
Invoke-Tool "$bt\zipalign.exe" -f -p 4 "$build\unsigned.apk" "$build\aligned.apk"

# 4. 署名 (鍵は初回に keystore\ に作る。同じ鍵で署名しないと上書きインストールできない)
$ksDir = Join-Path $root 'keystore'
$ks = "$ksDir\musicrpc.keystore"
$pass = "$ksDir\password.txt"
if (-not (Test-Path $ks)) {
    New-Item -ItemType Directory -Path $ksDir -Force | Out-Null
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($pass, [Convert]::ToBase64String($bytes))
    $pw = Get-Content $pass -Raw
    Invoke-Tool "$jdkPath\bin\keytool.exe" -genkeypair -keystore $ks -alias musicrpc -keyalg RSA -keysize 2048 `
        -validity 10000 -storepass $pw -keypass $pw -dname 'CN=MusicRPC Bridge'
}
Invoke-Tool "$bt\apksigner.bat" sign --ks $ks --ks-key-alias musicrpc --ks-pass "file:$pass" `
    --out "$build\MusicRPC-Bridge.apk" "$build\aligned.apk"

Write-Host ""
Write-Host "完成: $build\MusicRPC-Bridge.apk"
