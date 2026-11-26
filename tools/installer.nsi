Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma
CRCCheck force

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!define PRODUCT "Shioaji 測試精靈"
!define APP_DIR "app-${APP_VERSION}"
!define UNINSTALL_EXE "uninstall-shioaji-wizard.exe"
!define INSTALL_INFO ".shioaji-wizard-install.ini"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ShioajiWizard"

Name "${PRODUCT}"
OutFile "${OUTPUT_EXE}"
InstallDir "$LOCALAPPDATA\ShioajiWizard"
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${PRODUCT}"
VIAddVersionKey "FileDescription" "${PRODUCT} 安裝程式"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "Shioaji Wizard contributors"

!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_DIR}\wizard.exe"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "TradChinese"

Var NoIntegration
Var Upgrade

Function .onInit
  SetShellVarContext current
  ${GetParameters} $0
  ${GetOptions} "$0" "/NOINTEGRATION" $NoIntegration
  ${If} ${Errors}
    StrCpy $NoIntegration 0
  ${Else}
    StrCpy $NoIntegration 1
  ${EndIf}
FunctionEnd

Section
  SetOutPath "$INSTDIR\${APP_DIR}"
  File /a /r /x ".env" /x ".env.*" /x "*.pfx" /x "*.p12" /x "*.key" /x "*.ppk" /x "*.log" /x ".runtime" "${PAYLOAD}\*"
  File "${INSTALLER_MARKER}"

  ; 先放好新版本，再用上一版的出貨清單移除舊程式。連碰到未知檔案時也不遞迴刪除。
  ReadINIStr $0 "$INSTDIR\${INSTALL_INFO}" "ShioajiWizard" "Version"
  ${If} $0 != ""
  ${AndIf} $0 != "${APP_VERSION}"
  ${AndIf} ${FileExists} "$INSTDIR\${UNINSTALL_EXE}"
    InitPluginsDir
    ClearErrors
    CopyFiles /SILENT "$INSTDIR\${UNINSTALL_EXE}" "$PLUGINSDIR"
    ${IfNot} ${Errors}
      ExecWait '"$PLUGINSDIR\${UNINSTALL_EXE}" /S /NOINTEGRATION /UPGRADE _?=$INSTDIR'
    ${EndIf}
  ${EndIf}

  WriteUninstaller "$INSTDIR\${UNINSTALL_EXE}"
  WriteINIStr "$INSTDIR\${INSTALL_INFO}" "ShioajiWizard" "Version" "${APP_VERSION}"
  ${If} $NoIntegration == 0
    CreateShortcut "$SMPROGRAMS\${PRODUCT}.lnk" "$INSTDIR\${APP_DIR}\wizard.exe"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_DIR}\wizard.exe"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\${UNINSTALL_EXE}$\"'
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ${EndIf}
SectionEnd

Function un.onInit
  SetShellVarContext current
  ${un.GetParameters} $0
  ${un.GetOptions} "$0" "/NOINTEGRATION" $NoIntegration
  ${If} ${Errors}
    StrCpy $NoIntegration 0
  ${Else}
    StrCpy $NoIntegration 1
  ${EndIf}
  ${un.GetOptions} "$0" "/UPGRADE" $Upgrade
  ${If} ${Errors}
    StrCpy $Upgrade 0
  ${Else}
    StrCpy $Upgrade 1
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Delete "$INSTDIR\${APP_DIR}\.shioaji-wizard-installed"
  !include "${UNINSTALL_MANIFEST}"
  RMDir "$INSTDIR\${APP_DIR}"
  ${If} $Upgrade == 0
    Delete "$INSTDIR\${INSTALL_INFO}"
    Delete "$INSTDIR\${UNINSTALL_EXE}"
    RMDir "$INSTDIR"
  ${EndIf}
  ${If} $NoIntegration == 0
    ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
    ${If} $0 == "$INSTDIR"
      Delete "$SMPROGRAMS\${PRODUCT}.lnk"
      DeleteRegKey HKCU "${UNINSTALL_KEY}"
    ${EndIf}
  ${EndIf}
SectionEnd
