local vuxConfig = require "config"

NotifyOnNewObject("/Script/Pal.PalGameSetting", function(vuxPalGameSetting)
	vuxPalGameSetting.FlyHorizon_SP = vuxConfig.vuxFlyHorizon_SP
	vuxPalGameSetting.FlyHorizon_Dash_SP = vuxConfig.vuxFlyHorizon_Dash_SP
	vuxPalGameSetting.FlyHover_SP = vuxConfig.vuxFlyHover_SP
	vuxPalGameSetting.FlyMaxHeight = vuxConfig.vuxFlyMaxHeight
	vuxPalGameSetting.FlyVertical_SP = vuxConfig.vuxFlyVertical_SP
	print("Remove flying stamina applied")
end)


RegisterHook("/Script/Engine.PlayerController:ServerAcknowledgePossession", function(vuxPlayer)
	vuxPlayerPawn = vuxPlayer:get().Pawn
	if vuxPlayerPawn ~= previousVuxPlayerPawn then
		local items = FindAllOf("PalGameSetting")
		if items then
			for _, item in ipairs(items) do
				item.FlyHorizon_SP = vuxConfig.vuxFlyHorizon_SP
				item.FlyHorizon_Dash_SP = vuxConfig.vuxFlyHorizon_Dash_SP
				item.FlyHover_SP = vuxConfig.vuxFlyHover_SP
				item.FlyMaxHeight = vuxConfig.vuxFlyMaxHeight
				item.FlyVertical_SP = vuxConfig.vuxFlyVertical_SP
			end
		end
		print("Remove flying stamina applied")
	end
	previousVuxPlayerPawn = vuxPlayerPawn
end)

RegisterHook("/Script/Engine.PlayerController:ClientRestart", function()
	if vuxConfig.vuxCheckRestart ~= 1 then
		local items = FindAllOf("PalGameSetting")
		if items then
			for _, item in ipairs(items) do
				item.FlyHorizon_SP = vuxConfig.vuxFlyHorizon_SP
				item.FlyHorizon_Dash_SP = vuxConfig.vuxFlyHorizon_Dash_SP
				item.FlyHover_SP = vuxConfig.vuxFlyHover_SP
				item.FlyMaxHeight = vuxConfig.vuxFlyMaxHeight
				item.FlyVertical_SP = vuxConfig.vuxFlyVertical_SP
			end
		print("Remove flying stamina applied")
		end
		print("Remove flying stamina mod version " .. vuxConfig.vuxModVersion .. " loaded for game version " .. vuxConfig.vuxGameVersion)
	end
	vuxConfig.vuxCheckRestart = 1
end)