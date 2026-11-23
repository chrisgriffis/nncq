<!-- nncq uml export: generator=1.2 repo=orbitkit view=downlink-path view_rev=0ea498376d4d100e model_hash=0ea498376d4d100e -->
### How a frame becomes packets

```mermaid
classDiagram
  direction TB
  class n_pipe["Pipeline"]
  n_pipe : +Pipeline(link꞉꞉IRadio& radio, std꞉꞉unique_ptr~IDecoder~ decoder, core)
  n_pipe : +addSink(std꞉꞉shared_ptr~ISink~ sink) void
  n_pipe : +run(std꞉꞉size_t frameBudget) std꞉꞉size_t
  n_pipe : +reset() void
  n_pipe : -dispatch(const Packet& packet) void
  n_pipe : -account(const DecodeResult& result, const core꞉꞉Frame& frame) void
  n_pipe : -link꞉꞉IRadio& radio_
  n_pipe : -std꞉꞉unique_ptr~IDecoder~ decoder_
  n_pipe : -std꞉꞉vector~std꞉꞉shared_ptr~ISink~~ sinks_
  n_pipe : -core꞉꞉Clock& clock_
  n_pipe : -core꞉꞉RawBuffer scratch_
  n_pipe : -Stats stats_
  style n_pipe fill:#1b2a44,stroke:#5aa9ff,color:#d7dae0
  class n_radio["IRadio"]
  <<interface>> n_radio
  n_radio : +~IRadio()
  n_radio : +tune(double frequencyHz) bool*
  n_radio : +receive(core꞉꞉RawBuffer& out, std꞉꞉size_t maxBytes) std꞉꞉size_t*
  n_radio : +state() LinkState*
  n_radio : +name() const char∗*
  style n_radio fill:#2a1f44,stroke:#b28cff,color:#d7dae0
  class n_dec["IDecoder"]
  <<interface>> n_dec
  n_dec : +~IDecoder()
  n_dec : +decode(const core꞉꞉RawBuffer& raw, core꞉꞉Frame& frame) DecodeResult*
  n_dec : +protocol() const char∗*
  style n_dec fill:#2a1f44,stroke:#b28cff,color:#d7dae0
  class n_framed["FramedDecoder"]
  <<abstract>> n_framed
  n_framed : +FramedDecoder(std꞉꞉uint32_t syncMarker, std꞉꞉size_t frameSize)
  n_framed : #findSync(const core꞉꞉RawBuffer& raw) std꞉꞉size_t
  n_framed : +decode(const core꞉꞉RawBuffer& raw, core꞉꞉Frame& frame) DecodeResult
  n_framed : +crcFailures() std꞉꞉uint64_t
  n_framed : #decodeBody(const std꞉꞉uint8_t∗ body, std꞉꞉size_t size, core꞉꞉Frame& fra) DecodeResult*
  n_framed : #std꞉꞉uint32_t syncMarker_
  n_framed : #std꞉꞉size_t frameSize_
  n_framed : #std꞉꞉uint64_t crcFailures_
  style n_framed fill:#2a1f44,stroke:#b28cff,color:#d7dae0
  class n_ccsds["CcsdsDecoder"]
  n_ccsds : CcsdsDecoder(std꞉꞉size_t frameSize)
  n_ccsds : -parseHeader(const std꞉꞉uint8_t∗ body) core꞉꞉Frame꞉꞉Header$
  n_ccsds : +constexpr std꞉꞉uint32_t kAsm$
  n_ccsds : +CcsdsDecoder(std꞉꞉size_t frameSize = 223)
  n_ccsds : +protocol() const char∗
  n_ccsds : #decodeBody(const std꞉꞉uint8_t∗ body, std꞉꞉size_t size, core꞉꞉Frame& fra) DecodeResult
  n_ccsds : -extractPackets(const std꞉꞉uint8_t∗ data, std꞉꞉size_t size, DecodeResult& ou) std꞉꞉size_t
  n_ccsds : -std꞉꞉uint8_t lastSequence_
  n_ccsds : -std꞉꞉uint32_t sequenceGaps_
  style n_ccsds fill:#1b2a44,stroke:#5aa9ff,color:#d7dae0
  class n_crc["Crc"]
  n_crc : -table16() const std꞉꞉uint16_t∗$
  n_crc : +ccitt16(const std꞉꞉uint8_t∗ data, std꞉꞉size_t size, std꞉꞉uint16_t se) std꞉꞉uint16_t$
  n_crc : +crc32(const std꞉꞉uint8_t∗ data, std꞉꞉size_t size, std꞉꞉uint32_t se) std꞉꞉uint32_t$
  n_crc : +check(const std꞉꞉uint8_t∗ frame, std꞉꞉size_t size) bool$
  n_crc : ccitt16(const std꞉꞉uint8_t∗ data, std꞉꞉size_t size, std꞉꞉uint16_t se) std꞉꞉uint16_t
  n_crc : crc32(const std꞉꞉uint8_t∗ data, std꞉꞉size_t size, std꞉꞉uint32_t se) std꞉꞉uint32_t
  style n_crc fill:#1b2a44,stroke:#5aa9ff,color:#d7dae0
  class n_sink["ISink"]
  <<abstract>> n_sink
  n_sink : +~ISink()
  n_sink : +write(const Packet& packet) void*
  n_sink : +flush() void
  style n_sink fill:#2a1f44,stroke:#b28cff,color:#d7dae0
  class n_sdr["SDR driver"]
  <<external>> n_sdr
  style n_sdr fill:#17191d,stroke:#5a6375,color:#d7dae0
  n_framed ..> n_crc : check()
  n_radio ..> n_sdr : samples
  n_framed <|-- n_ccsds
  n_dec <|.. n_framed
  n_pipe --> "1" n_radio : radio_
  n_pipe *-- "1" n_dec : decoder_
  n_pipe o-- "*" n_sink : sinks_
  click n_pipe href "http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline?view=downlink-path&v=0ea498376d4d100e&h=ff1f8df964&repo=orbitkit" _blank
  click n_radio href "http://127.0.0.1:8765/views#/uml/interface/sym%3Aorbitkit%3A%3Alink%3A%3AIRadio?view=downlink-path&v=0ea498376d4d100e&h=62da90376b&repo=orbitkit" _blank
  click n_dec href "http://127.0.0.1:8765/views#/uml/interface/sym%3Aorbitkit%3A%3Atelemetry%3A%3AIDecoder?view=downlink-path&v=0ea498376d4d100e&h=1a4aca51f7&repo=orbitkit" _blank
  click n_framed href "http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder?view=downlink-path&v=0ea498376d4d100e&h=7e0bb49180&repo=orbitkit" _blank
  click n_ccsds href "http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder?view=downlink-path&v=0ea498376d4d100e&h=1b7d2d993e&repo=orbitkit" _blank
  click n_crc href "http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Acore%3A%3ACrc?view=downlink-path&v=0ea498376d4d100e&h=a393d804e2&repo=orbitkit" _blank
  click n_sink href "http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Atelemetry%3A%3AISink?view=downlink-path&v=0ea498376d4d100e&h=35a9d61d77&repo=orbitkit" _blank
%% made via mcp · copilot-cli:claude-opus-5.5
```

Elements ([open this view](http://127.0.0.1:8765/views#/uml/view/view%3Adownlink-path?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit)):

- [Pipeline](http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline?view=downlink-path&v=0ea498376d4d100e&h=ff1f8df964&repo=orbitkit) class `libs/telemetry/include/orbitkit/telemetry/Pipeline.h:17`
  - [Pipeline()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3APipeline?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Pipeline.cpp:15`
  - [addSink()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3AaddSink?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Pipeline.cpp:21`
  - [run()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Arun?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Pipeline.cpp:26`
  - [reset()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Areset?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Pipeline.cpp:66`
  - [dispatch()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Adispatch?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Pipeline.cpp:49`
  - [account()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Aaccount?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Pipeline.cpp:55`
  - [radio_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Aradio_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Pipeline.h:42`
  - [decoder_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Adecoder_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Pipeline.h:43`
  - [sinks_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Asinks_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Pipeline.h:44`
  - [clock_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Aclock_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Pipeline.h:45`
  - [scratch_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Ascratch_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Pipeline.h:46`
  - [stats_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3APipeline%3A%3Astats_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Pipeline.h:47`
- [IRadio](http://127.0.0.1:8765/views#/uml/interface/sym%3Aorbitkit%3A%3Alink%3A%3AIRadio?view=downlink-path&v=0ea498376d4d100e&h=62da90376b&repo=orbitkit) interface `libs/link/include/orbitkit/link/Radio.h:11`
  - [~IRadio()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Alink%3A%3AIRadio%3A%3A~IRadio?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/link/include/orbitkit/link/Radio.h:13`
  - [tune()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Alink%3A%3AIRadio%3A%3Atune?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/link/include/orbitkit/link/Radio.h:15`
  - [receive()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Alink%3A%3AIRadio%3A%3Areceive?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/link/include/orbitkit/link/Radio.h:17`
  - [state()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Alink%3A%3AIRadio%3A%3Astate?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/link/include/orbitkit/link/Radio.h:18`
  - [name()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Alink%3A%3AIRadio%3A%3Aname?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/link/include/orbitkit/link/Radio.h:19`
- [IDecoder](http://127.0.0.1:8765/views#/uml/interface/sym%3Aorbitkit%3A%3Atelemetry%3A%3AIDecoder?view=downlink-path&v=0ea498376d4d100e&h=1a4aca51f7&repo=orbitkit) interface `libs/telemetry/include/orbitkit/telemetry/Decoder.h:21`
  - [~IDecoder()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AIDecoder%3A%3A~IDecoder?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:23`
  - [decode()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AIDecoder%3A%3Adecode?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:25`
  - [protocol()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AIDecoder%3A%3Aprotocol?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:26`
- [FramedDecoder](http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder?view=downlink-path&v=0ea498376d4d100e&h=7e0bb49180&repo=orbitkit) class `libs/telemetry/include/orbitkit/telemetry/Decoder.h:31`
  - [FramedDecoder()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3AFramedDecoder?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Decoder.cpp:7`
  - [findSync()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3AfindSync?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Decoder.cpp:12`
  - [decode()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3Adecode?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/Decoder.cpp:23`
  - [crcFailures()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3AcrcFailures?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:37`
  - [decodeBody()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3AdecodeBody?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:40`
  - [syncMarker_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3AsyncMarker_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:44`
  - [frameSize_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3AframeSize_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:45`
  - [crcFailures_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3AFramedDecoder%3A%3AcrcFailures_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/Decoder.h:46`
- [CcsdsDecoder](http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder?view=downlink-path&v=0ea498376d4d100e&h=1b7d2d993e&repo=orbitkit) class `libs/telemetry/include/orbitkit/telemetry/CcsdsDecoder.h:7`
  - [CcsdsDecoder()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3ACcsdsDecoder?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/CcsdsDecoder.cpp:5`
  - [parseHeader()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3AparseHeader?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/CcsdsDecoder.cpp:7`
  - [kAsm](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3AkAsm?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/CcsdsDecoder.h:9`
  - [CcsdsDecoder()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3ACcsdsDecoder?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/CcsdsDecoder.h:11`
  - [protocol()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3Aprotocol?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/CcsdsDecoder.h:13`
  - [decodeBody()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3AdecodeBody?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/CcsdsDecoder.cpp:37`
  - [extractPackets()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3AextractPackets?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/src/CcsdsDecoder.cpp:17`
  - [lastSequence_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3AlastSequence_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/CcsdsDecoder.h:22`
  - [sequenceGaps_](http://127.0.0.1:8765/views#/uml/member/sym%3Aorbitkit%3A%3Atelemetry%3A%3ACcsdsDecoder%3A%3AsequenceGaps_?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/telemetry/include/orbitkit/telemetry/CcsdsDecoder.h:23`
- [Crc](http://127.0.0.1:8765/views#/uml/class/sym%3Aorbitkit%3A%3Acore%3A%3ACrc?view=downlink-path&v=0ea498376d4d100e&h=a393d804e2&repo=orbitkit) class `libs/core/include/orbitkit/core/Crc.h:9`
  - [table16()](http://127.0.0.1:8765/views#/uml/method/sym%3Aorbitkit%3A%3Acore%3A%3ACrc%3A%3Atable16?view=downlink-path&v=0ea498376d4d100e&repo=orbitkit) `libs/core/src/Crc.cpp:7`