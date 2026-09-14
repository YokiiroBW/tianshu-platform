// Platform-owned bridge to the fixed, unmodified AssetLibrary test fixture assembly.
// Reflection only accesses fixture setup helpers; grants use the public operator.
using System.Reflection;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json.Nodes;
using AssetLibrary.WebGateway.Tests;
using AssetLibrary.Modules.LibraryStorage.Contracts;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

[assembly: Microsoft.VisualStudio.TestTools.UnitTesting.DoNotParallelize]

static class Probe
{
    static readonly Assembly Fixtures = typeof(ServiceReadIntegrationTests).Assembly;
    static Type Type(string name) => Fixtures.GetType("AssetLibrary.WebGateway.Tests." + name, true)!;
    static object Get(object value, string name) => value.GetType().GetProperty(name)!.GetValue(value)!;
    static async Task<object?> Call(string type, string method, object? target, params object?[] args)
    {
        var member = Type(type).GetMethod(method)!;
        var parameters = member.GetParameters();
        var fullArgs = parameters.Select((p, i) => i < args.Length ? args[i] : p.DefaultValue).ToArray();
        var value = member.Invoke(target, fullArgs);
        if (value is Task task)
        {
            await task;
            return task.GetType().GetProperty("Result")?.GetValue(task);
        }
        return value;
    }
    static void Reply(JsonObject value) => Console.WriteLine("TS064:" + value.ToJsonString());
    static async Task<JsonObject> Manage(object host, string action, Guid principal, JsonObject? fields = null)
    {
        return (JsonObject)(await Call("ServiceReadIntegrationHttp", "ManageAsync", null, host, action, principal, fields, 0))!;
    }
    static JsonObject Libraries(Guid id) => new() { ["library_ids"] = new JsonArray(id.ToString()) };
    public static async Task<int> Main()
    {
        var settings = await Call("TrialHostIntegrationSettings", "Load", null);
        var runtime = (string)Get(settings!, "RuntimeRoot");
        var assets = Activator.CreateInstance(Type("NativeClientSampleAssets"), runtime, null)!;
        var sourceRoot = Path.GetFullPath((string)Get(assets, "LibraryRoot"));
        var offlineRoot = sourceRoot + ".offline";
        var allowed = Path.GetFullPath(Path.Combine(runtime, "assets")) + Path.DirectorySeparatorChar;
        if (!sourceRoot.StartsWith(allowed, StringComparison.OrdinalIgnoreCase)
            || !offlineRoot.StartsWith(allowed, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Synthetic move escaped fixture root");
        var host = (await Call("TrialHostIntegrationFixture", "CreateAsync", null, settings, null, true))!;
        try
        {
            var library = (Guid)(await Call("NativeClientFixtureSeed", "PrepareAsync", null, host, assets))!;
            var admin = (await Call("TrialHostIntegrationHttp", "SignInAsync", null, host))!;
            var secondRoot = Directory.CreateDirectory(Path.Combine(runtime, "assets", "second")).FullName;
            await File.WriteAllTextAsync(Path.Combine(secondRoot, "第二库_中文.txt"), "synthetic second original");
            var registered = (await Call("TrialHostIntegrationHttp", "ControlAsync", null, host, admin, "libraries.register", new JsonObject {
                ["root_path"] = secondRoot, ["display_name"] = "TS064 second library", ["source_key"] = "fixtures"
            }, (Guid?)Guid.NewGuid()))!;
            var second = ((JsonObject)Get(registered, "Payload"))["body"]!["library_id"]!.GetValue<Guid>();
            var secondBody = new JsonObject { ["library_id"] = second.ToString() };
            await Call("TrialHostIntegrationHttp", "ControlAsync", null, host, admin, "library_scans.start", secondBody, (Guid?)Guid.NewGuid());
            using (var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(45)))
            {
                while (true)
                {
                    var progress = (await Call("TrialHostIntegrationHttp", "ControlAsync", null, host, admin, "library_scans.get", secondBody, null))!;
                    var state = ((JsonObject)Get(progress, "Payload"))["body"]!["scan"]!["state"]!.GetValue<string>();
                    if (state == "succeeded") break;
                    if (state is not ("queued" or "leased")) throw new InvalidOperationException("Second scan failed");
                    await Task.Delay(100, deadline.Token);
                }
            }
            var principals = new[] { Guid.NewGuid(), Guid.NewGuid() };
            var tokens = new JsonArray();
            for (var index = 0; index < 2; index++)
            {
                await Manage(host, "service-create", principals[index], new JsonObject { ["display_name"] = "TS064 service " + index });
                await Manage(host, "service-grant", principals[index], Libraries(index == 0 ? library : second));
                tokens.Add(await Manage(host, "service-issue", principals[index]));
            }
            var ca = Path.Combine(runtime, "platform-ca.pem");
            await File.WriteAllTextAsync(ca, ((X509Certificate2)Get(host, "Certificate")).ExportCertificatePem());
            var configuration = Get(host, "Configuration");
            Reply(new JsonObject { ["ready"] = true, ["endpoint"] = Get(configuration, "PublicOrigin") + "/assetlink/v1/control",
                ["ca_file"] = ca, ["libraries"] = new JsonArray(library.ToString(), second.ToString()), ["credentials"] = tokens });
            while (await Console.In.ReadLineAsync() is { } line)
            {
                var command = JsonNode.Parse(line)!.AsObject();
                var action = command["action"]!.GetValue<string>();
                if (action == "stop") break;
                if (action == "restart")
                {
                    await Call("TrialHostIntegrationFixture", "RestartAsync", host);
                    Reply(new JsonObject { ["ok"] = true });
                }
                else if (action == "pause")
                {
                    var application = (IHost)host.GetType().GetField("application", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(host)!;
                    await application.StopAsync();
                    Reply(new JsonObject { ["ok"] = true });
                }
                else if (action is "source-offline" or "source-online")
                {
                    var offline = action == "source-offline";
                    Directory.Move(offline ? sourceRoot : offlineRoot, offline ? offlineRoot : sourceRoot);
                    var availability = ((IServiceProvider)Get(host, "Services")).GetRequiredService<ILibraryAvailability>();
                    var state = await availability.RefreshAsync(new LibraryId(library), CancellationToken.None);
                    Reply(new JsonObject { ["availability"] = state.ToString().ToLowerInvariant() });
                }
                else
                {
                    var index = command["principal"]!.GetValue<int>();
                    Reply(await Manage(host, action, principals[index], command["fields"]?.AsObject()));
                }
            }
            await Call("NativeClientSampleAssets", "VerifyUnchanged", assets);
            if (await File.ReadAllTextAsync(Path.Combine(secondRoot, "第二库_中文.txt")) != "synthetic second original")
                throw new InvalidOperationException("Second original changed");
        }
        finally
        {
            if (Directory.Exists(offlineRoot)) Directory.Move(offlineRoot, sourceRoot);
            await ((IAsyncDisposable)host).DisposeAsync();
        }
        Reply(new JsonObject { ["stopped"] = true, ["originals_unchanged"] = true });
        return 0;
    }
}
