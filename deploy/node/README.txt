UNREAL RENDER FARM - RENDER NODE PACKAGE
======================================

Install on a render machine:
  1. Copy this whole folder to the render machine (Desktop or anywhere).
  2. Double-click SETUP.bat and click "Yes" when Windows asks for admin rights.
  3. Wait for "Done. This render node is ready." - the machine appears in the dashboard by itself.

That's it. SETUP.bat installs to C:\UnrealRenderFarm\node, installs Python if needed (bundled, no
internet required), finds Unreal Engine, starts the agent, and registers the machine with the
master. Afterwards you can delete the copied folder.

Optional - before step 2, edit farm.env if:
  - projects live on a network path like \\server\share  ->  URF_PROJECT_ROOTS=\\server\share\Projects
  - Unreal is somewhere unusual and setup can't find it    ->  URF_UE_EXE=...\UnrealEditor-Cmd.exe

Update a node:   copy a newer package and run SETUP.bat again (settings made on the node are kept).
Remove a node:   run UNINSTALL.bat.
Logs:            C:\UnrealRenderFarm\logs   (setup.log, farm_agent.*.log)
Render logs:     C:\UnrealRenderFarm\agent\logs

For unattended rendering after a reboot, set the machine to log on automatically
(e.g. Sysinternals Autologon) - Unreal needs a logged-on desktop to use the GPU.

Keep this folder private: farm.env contains the farm's secret token.
