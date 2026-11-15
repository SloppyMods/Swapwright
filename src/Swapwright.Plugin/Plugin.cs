using Dalamud.Game.Command;
using Dalamud.Interface.Windowing;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;
using Swapwright.Core;

namespace Swapwright.Plugin;

public sealed class Plugin : IDalamudPlugin
{
    private const string Command = "/swapwright";
    private const string ShortCommand = "/sw";

    private readonly IDalamudPluginInterface _pi;
    private readonly ICommandManager _commands;
    private readonly IFramework _framework;
    private readonly Configuration _config;
    private readonly PenumbraGateway _penumbra;
    private readonly SwapdApi _swapd;
    private readonly TestSession _test;
    private readonly WindowSystem _windows = new("Swapwright");
    private readonly MainWindow _main;

    public Plugin(IDalamudPluginInterface pi, ICommandManager commands, IFramework framework,
        IDataManager data, IPluginLog log)
    {
        _pi = pi;
        _commands = commands;
        _framework = framework;
        _config = pi.GetPluginConfig() as Configuration ?? new Configuration();

        _penumbra = new PenumbraGateway(pi, log, OnPenumbraInitialized, OnPenumbraDisposed);
        _test = new TestSession(_penumbra);
        _swapd = SwapdClient.Create(_config.SwapdPort);

        _main = new MainWindow(_config, () => pi.SavePluginConfig(_config), _swapd, _penumbra, _test, framework, data, log);
        _windows.AddWindow(_main);

        pi.UiBuilder.Draw += _windows.Draw;
        pi.UiBuilder.OpenMainUi += _main.Toggle;
        _commands.AddHandler(Command, new CommandInfo((_, _) => _main.Toggle())
        {
            HelpMessage = "Open or close the Swapwright window.",
        });
        _commands.AddHandler(ShortCommand, new CommandInfo((_, _) => _main.Toggle())
        {
            HelpMessage = "Short for /swapwright.",
        });
    }

    // Penumbra can raise Initialized from inside the PenumbraGateway constructor, before _main exists.
    private void OnPenumbraInitialized() => _framework.RunOnFrameworkThread(() => _main?.RequestRefresh());

    // Penumbra's temporary mods die with it: forget ours without calling it.
    private void OnPenumbraDisposed() => _framework.RunOnFrameworkThread(() => _test?.Forget());

    public void Dispose()
    {
        _commands.RemoveHandler(Command);
        _commands.RemoveHandler(ShortCommand);
        _pi.UiBuilder.Draw -= _windows.Draw;
        _pi.UiBuilder.OpenMainUi -= _main.Toggle;
        _windows.RemoveAllWindows();

        _main.Dispose();
        _test.Dispose(); // removes the test temp mod (D8)
        _penumbra.Dispose();
        _swapd.Dispose();
    }
}
