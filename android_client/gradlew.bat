@echo off
setlocal
set "APP_HOME=%~dp0"
set "WRAPPER_JAR=%APP_HOME%gradle\wrapper\gradle-wrapper.jar"
set "WRAPPER_URL=https://raw.githubusercontent.com/gradle/gradle/v8.11.1/gradle/wrapper/gradle-wrapper.jar"
set "WRAPPER_SHA256=2db75c40782f5e8ba1fc278a5574bab070adccb2d21ca5a6e5ed840888448046"

if exist "%WRAPPER_JAR%" goto verify

goto download

:verify
for /f %%H in ('powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath ''%WRAPPER_JAR%'').Hash.ToLower()"') do set "ACTUAL_SHA=%%H"
if /I "%ACTUAL_SHA%"=="%WRAPPER_SHA256%" goto run

del /q "%WRAPPER_JAR%" >nul 2>&1

goto download

:download
echo Downloading verified Gradle wrapper 8.11.1...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '%WRAPPER_URL%' -OutFile '%WRAPPER_JAR%'"
if errorlevel 1 exit /b 1
for /f %%H in ('powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath ''%WRAPPER_JAR%'').Hash.ToLower()"') do set "ACTUAL_SHA=%%H"
if /I not "%ACTUAL_SHA%"=="%WRAPPER_SHA256%" (
  echo Gradle wrapper SHA-256 verification failed.
  del /q "%WRAPPER_JAR%" >nul 2>&1
  exit /b 1
)

:run
if defined JAVA_HOME (
  set "JAVACMD=%JAVA_HOME%\bin\java.exe"
) else (
  set "JAVACMD=java.exe"
)

"%JAVACMD%" %JAVA_OPTS% %GRADLE_OPTS% -Dorg.gradle.appname=gradlew -classpath "%WRAPPER_JAR%" org.gradle.wrapper.GradleWrapperMain %*
exit /b %ERRORLEVEL%
