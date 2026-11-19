// offload-edge.cc: ns-3 <-> external recommender, with edge queues and processing.
//
// Topology (star, wired point-to-point for now):
//
//   user0 ---\                /--- edge0  (fast, e.g. GPU)
//   user1 ----+-- basestation-+--- edge1
//   userN ---/                \--- edgeM
//
// For every task:
//   1. ns-3 PAUSES and asks the external recommender (TCP, JSON lines) which edge to use.
//   2. The task is sent as one UDP packet to that edge.
//   3. The edge puts it in a bounded FIFO queue and processes tasks one at a time.
//      Processing time = task work (Gcycles) / edge speed (GHz).
//   4. ns-3 reports the outcome back to the recommender:
//        met        finished before its deadline
//        queue_full rejected because the edge queue was full   (dropPolicy=tail)
//        evicted    pushed out of a full queue by a newer task  (dropPolicy=oldest)
//        expired    still queued or processing when the deadline passed
//        lost       never reached the edge before the deadline (packet loss)
//
// Every task gets exactly one feedback message. Because ns-3 is discrete-event,
// simulated time does not advance while we wait for the recommender.
//
// Run:  python3 recommender.py &   then   ./ns3 run "offload-edge --nUsers=10"

#include "ns3/applications-module.h" // SeqTsHeader
#include "ns3/core-module.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"

#include <algorithm>
#include <arpa/inet.h>
#include <deque>
#include <map>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sstream>
#include <string>
#include <sys/socket.h>
#include <unistd.h>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("OffloadEdge");

// ---------- external pipe (plain POSIX TCP, one JSON object per line) ----------
static int g_fd = -1;
static std::string g_rxBuf;

static void
PipeConnect(uint16_t port)
{
    g_fd = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    for (int i = 0; i < 100; ++i) // retry ~10 s while the recommender starts up
    {
        if (connect(g_fd, (sockaddr*)&addr, sizeof(addr)) == 0)
        {
            // Send each small JSON line immediately. Without this, Nagle's algorithm holds a
            // request back until the previous feedback line is ACKed, and with Linux delayed
            // ACKs that stalls every task by ~40 ms of wall-clock time.
            int one = 1;
            setsockopt(g_fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
            return;
        }
        usleep(100000);
    }
    NS_FATAL_ERROR("Could not reach recommender on 127.0.0.1:" << port
                                                               << " (is recommender.py running?)");
}

static void
PipeSend(const std::string& line)
{
    std::string s = line + "\n";
    size_t off = 0;
    while (off < s.size())
    {
        ssize_t n = send(g_fd, s.data() + off, s.size() - off, 0);
        if (n <= 0)
        {
            NS_FATAL_ERROR("Recommender pipe closed");
        }
        off += n;
    }
}

static std::string
PipeReadLine()
{
    char buf[4096];
    size_t nl;
    while ((nl = g_rxBuf.find('\n')) == std::string::npos)
    {
        ssize_t n = recv(g_fd, buf, sizeof(buf), 0);
        if (n <= 0)
        {
            NS_FATAL_ERROR("Recommender closed the connection without replying");
        }
        g_rxBuf.append(buf, n);
    }
    std::string line = g_rxBuf.substr(0, nl);
    g_rxBuf.erase(0, nl + 1);
    return line;
}

// ---------- simulation state ----------
struct Task
{
    uint32_t user;
    uint32_t edge;
    double created;  // when the task was generated (s)
    double deadline; // absolute deadline (s)
    double work;     // Gcycles
    bool arrived = false;
    double arrivedAt = 0;
    double startedAt = -1;
};

struct Edge
{
    double ghz;
    std::deque<uint32_t> queue; // waiting tasks (not including the one in service)
    bool busy = false;
    uint32_t current = 0;
    EventId doneEvent;
};

static std::map<uint32_t, Task> g_tasks; // tasks without an outcome yet
static std::vector<Edge> g_edges;
static std::map<std::string, uint32_t> g_outcomes;
static uint32_t g_nextTaskId = 0;

static Ptr<ExponentialRandomVariable> g_interArrival;
static Ptr<UniformRandomVariable> g_workRv;
static std::vector<Ptr<Socket>> g_userSockets;
static std::vector<Ipv4Address> g_edgeAddrs;
static uint32_t g_taskSize;
static double g_taskWork;
static double g_deadline;
static double g_simTime;
static uint32_t g_queueCap;
static bool g_dropOldest;
static const uint16_t kEdgePort = 9000;

static uint32_t
EdgeLoad(const Edge& e)
{
    return e.queue.size() + (e.busy ? 1 : 0);
}

// Send the single feedback message for a task and forget it.
static void
Report(uint32_t id, const std::string& outcome)
{
    auto it = g_tasks.find(id);
    if (it == g_tasks.end())
    {
        return; // already reported
    }
    const Task& t = it->second;
    double now = Simulator::Now().GetSeconds();
    bool met = outcome == "met";

    std::ostringstream fb;
    fb << R"({"type":"feedback","task_id":)" << id << R"(,"user":)" << t.user << R"(,"edge":)"
       << t.edge << R"(,"outcome":")" << outcome << R"(","met_deadline":)"
       << (met ? "true" : "false");
    fb << R"(,"delay":)";
    met ? fb << (now - t.created) : fb << "null";
    fb << R"(,"net_delay":)";
    t.arrived ? fb << (t.arrivedAt - t.created) : fb << "null";
    fb << R"(,"queue_wait":)";
    t.startedAt >= 0 ? fb << (t.startedAt - t.arrivedAt) : fb << "null";
    fb << R"(,"edge_load":)" << EdgeLoad(g_edges[t.edge]) << "}";
    PipeSend(fb.str());

    g_outcomes[outcome]++;
    g_tasks.erase(it);
}

static void StartNext(uint32_t e);

static void
FinishProcessing(uint32_t e)
{
    Edge& edge = g_edges[e];
    uint32_t id = edge.current;
    edge.busy = false;
    auto it = g_tasks.find(id);
    if (it != g_tasks.end())
    {
        Report(id, Simulator::Now().GetSeconds() <= it->second.deadline + 1e-12 ? "met"
                                                                                : "expired");
    }
    StartNext(e);
}

static void
StartNext(uint32_t e)
{
    Edge& edge = g_edges[e];
    if (edge.busy || edge.queue.empty())
    {
        return;
    }
    uint32_t id = edge.queue.front();
    edge.queue.pop_front();
    Task& t = g_tasks.at(id);
    t.startedAt = Simulator::Now().GetSeconds();
    edge.busy = true;
    edge.current = id;
    edge.doneEvent = Simulator::Schedule(Seconds(t.work / edge.ghz), &FinishProcessing, e);
}

// Called at each task's deadline: anything not finished by then has failed.
static void
CheckDeadline(uint32_t id)
{
    auto it = g_tasks.find(id);
    if (it == g_tasks.end())
    {
        return; // already has an outcome
    }
    Task& t = it->second;
    if (!t.arrived)
    {
        Report(id, "lost");
        return;
    }
    uint32_t e = t.edge; // copy: Report() erases the task, so t dangles after it
    Edge& edge = g_edges[e];
    if (edge.busy && edge.current == id)
    {
        // Abort: no point finishing a task whose deadline has passed.
        Simulator::Cancel(edge.doneEvent);
        edge.busy = false;
        Report(id, "expired");
        StartNext(e);
        return;
    }
    edge.queue.erase(std::remove(edge.queue.begin(), edge.queue.end(), id), edge.queue.end());
    Report(id, "expired");
}

static void
EdgeReceive(uint32_t e, Ptr<Socket> sock)
{
    Ptr<Packet> p;
    while ((p = sock->Recv()))
    {
        SeqTsHeader hdr;
        p->RemoveHeader(hdr);
        uint32_t id = hdr.GetSeq();
        auto it = g_tasks.find(id);
        if (it == g_tasks.end())
        {
            continue; // arrived after its deadline was already reported
        }
        Task& t = it->second;
        t.arrived = true;
        t.arrivedAt = Simulator::Now().GetSeconds();

        Edge& edge = g_edges[e];
        if (edge.queue.size() >= g_queueCap)
        {
            if (!g_dropOldest)
            {
                Report(id, "queue_full"); // drop-tail: reject the newcomer
                continue;
            }
            uint32_t victim = edge.queue.front(); // drop-oldest: evict head of queue
            edge.queue.pop_front();
            Report(victim, "evicted");
        }
        edge.queue.push_back(id);
        StartNext(e);
    }
}

static void
GenerateTask(uint32_t user)
{
    uint32_t id = g_nextTaskId++;
    double now = Simulator::Now().GetSeconds();
    double work = g_workRv->GetValue(0.5 * g_taskWork, 1.5 * g_taskWork);

    // 1. Ask the recommender (simulation is paused while we wait).
    //    "queues" and "edge_ghz" describe the edges right now; policies may ignore them.
    std::ostringstream req;
    req << R"({"type":"request","task_id":)" << id << R"(,"user":)" << user << R"(,"t":)" << now
        << R"(,"size":)" << g_taskSize << R"(,"work":)" << work << R"(,"deadline":)"
        << g_deadline << R"(,"queues":[)";
    for (uint32_t e = 0; e < g_edges.size(); ++e)
    {
        req << (e ? "," : "") << EdgeLoad(g_edges[e]);
    }
    req << R"(],"edge_ghz":[)";
    for (uint32_t e = 0; e < g_edges.size(); ++e)
    {
        req << (e ? "," : "") << g_edges[e].ghz;
    }
    req << "]}";
    PipeSend(req.str());
    std::string reply = PipeReadLine();

    auto pos = reply.find("\"edge\"");
    NS_ABORT_MSG_IF(pos == std::string::npos, "Bad recommender reply: " << reply);
    uint32_t edge = std::stoul(reply.substr(reply.find(':', pos) + 1));
    NS_ABORT_MSG_IF(edge >= g_edges.size(), "Recommender returned invalid edge " << edge);

    // 2. Offload: send the task to the chosen edge.
    SeqTsHeader hdr;
    hdr.SetSeq(id);
    Ptr<Packet> p = Create<Packet>(g_taskSize - hdr.GetSerializedSize());
    p->AddHeader(hdr);
    g_tasks[id] = Task{user, edge, now, now + g_deadline, work};
    g_userSockets[user]->SendTo(p, 0, InetSocketAddress(g_edgeAddrs[edge], kEdgePort));
    Simulator::Schedule(Seconds(g_deadline), &CheckDeadline, id);

    // 3. Schedule this user's next task.
    double next = g_interArrival->GetValue();
    if (now + next < g_simTime)
    {
        Simulator::Schedule(Seconds(next), &GenerateTask, user);
    }
}

static std::vector<double>
ParseList(const std::string& s)
{
    std::vector<double> v;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ','))
    {
        v.push_back(std::stod(item));
    }
    return v;
}

int
main(int argc, char* argv[])
{
    uint32_t nUsers = 10;
    uint32_t nEdges = 3;
    double meanInterval = 0.3;        // seconds between tasks, per user
    double lossRate = 0.0;            // packet error rate on edge links
    std::string edgeGhz = "4,1,1";    // CPU speed per edge; last value repeats
    std::string dropPolicy = "tail";  // tail | oldest
    uint16_t port = 5555;
    g_taskSize = 1000; // bytes, keep <= 1400 (one packet per task for now)
    g_taskWork = 0.1;  // mean Gcycles per task (uniform 0.5x..1.5x)
    g_deadline = 0.2;  // seconds
    g_simTime = 30.0;
    g_queueCap = 5;

    CommandLine cmd(__FILE__);
    cmd.AddValue("nUsers", "Number of user devices", nUsers);
    cmd.AddValue("nEdges", "Number of edge nodes", nEdges);
    cmd.AddValue("simTime", "Simulated seconds of task generation", g_simTime);
    cmd.AddValue("meanInterval", "Mean seconds between tasks per user", meanInterval);
    cmd.AddValue("taskSize", "Task size in bytes (64..1400)", g_taskSize);
    cmd.AddValue("taskWork", "Mean task work in Gcycles", g_taskWork);
    cmd.AddValue("deadline", "Task deadline in seconds", g_deadline);
    cmd.AddValue("edgeGhz", "Comma-separated edge CPU speeds in GHz", edgeGhz);
    cmd.AddValue("queueCap", "Max waiting tasks per edge", g_queueCap);
    cmd.AddValue("dropPolicy", "Full-queue policy: tail or oldest", dropPolicy);
    cmd.AddValue("lossRate", "Packet loss probability on edge links", lossRate);
    cmd.AddValue("port", "Recommender TCP port", port);
    cmd.Parse(argc, argv); // also accepts --RngRun=N for independent seeds
    NS_ABORT_MSG_IF(g_taskSize > 1400 || g_taskSize < 64, "taskSize must be 64..1400");
    NS_ABORT_MSG_IF(dropPolicy != "tail" && dropPolicy != "oldest",
                    "dropPolicy must be tail or oldest");
    NS_ABORT_MSG_IF(g_queueCap < 1, "queueCap must be >= 1");
    g_dropOldest = dropPolicy == "oldest";

    std::vector<double> speeds = ParseList(edgeGhz);
    NS_ABORT_MSG_IF(speeds.empty(), "edgeGhz is empty");
    for (uint32_t e = 0; e < nEdges; ++e)
    {
        Edge edge;
        edge.ghz = speeds[std::min<size_t>(e, speeds.size() - 1)];
        g_edges.push_back(edge);
    }

    NodeContainer bs, users, edges;
    bs.Create(1);
    users.Create(nUsers);
    edges.Create(nEdges);
    InternetStackHelper stack;
    stack.Install(bs);
    stack.Install(users);
    stack.Install(edges);

    PointToPointHelper userLink, edgeLink;
    userLink.SetDeviceAttribute("DataRate", StringValue("100Mbps"));
    userLink.SetChannelAttribute("Delay", StringValue("5ms"));
    edgeLink.SetDeviceAttribute("DataRate", StringValue("1Gbps"));
    edgeLink.SetChannelAttribute("Delay", StringValue("1ms"));

    Ipv4AddressHelper ip;
    ip.SetBase("10.1.0.0", "255.255.255.0");
    for (uint32_t i = 0; i < nUsers; ++i)
    {
        NetDeviceContainer d = userLink.Install(users.Get(i), bs.Get(0));
        ip.Assign(d);
        ip.NewNetwork();
        Ptr<Socket> s = Socket::CreateSocket(users.Get(i), UdpSocketFactory::GetTypeId());
        s->Bind();
        g_userSockets.push_back(s);
    }
    for (uint32_t e = 0; e < nEdges; ++e)
    {
        NetDeviceContainer d = edgeLink.Install(bs.Get(0), edges.Get(e));
        if (lossRate > 0)
        {
            Ptr<RateErrorModel> em = CreateObject<RateErrorModel>();
            em->SetAttribute("ErrorRate", DoubleValue(lossRate));
            em->SetAttribute("ErrorUnit", StringValue("ERROR_UNIT_PACKET"));
            d.Get(1)->SetAttribute("ReceiveErrorModel", PointerValue(em));
        }
        Ipv4InterfaceContainer ifc = ip.Assign(d);
        ip.NewNetwork();
        g_edgeAddrs.push_back(ifc.GetAddress(1));
        Ptr<Socket> s = Socket::CreateSocket(edges.Get(e), UdpSocketFactory::GetTypeId());
        s->Bind(InetSocketAddress(Ipv4Address::GetAny(), kEdgePort));
        s->SetRecvCallback(MakeBoundCallback(&EdgeReceive, e));
    }
    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    g_interArrival = CreateObject<ExponentialRandomVariable>();
    g_interArrival->SetAttribute("Mean", DoubleValue(meanInterval));
    g_workRv = CreateObject<UniformRandomVariable>();
    for (uint32_t u = 0; u < nUsers; ++u)
    {
        Simulator::Schedule(Seconds(1.0 + g_interArrival->GetValue()), &GenerateTask, u);
    }

    PipeConnect(port);
    Simulator::Stop(Seconds(g_simTime + g_deadline + 1.0)); // every task resolved by then
    Simulator::Run();
    Simulator::Destroy();
    close(g_fd);

    std::cout << "Tasks generated: " << g_nextTaskId << "\n";
    for (const auto& [outcome, n] : g_outcomes)
    {
        std::cout << "  " << outcome << ": " << n << "\n";
    }
    std::cout << "Unresolved:      " << g_tasks.size() << "\n";
    return 0;
}
