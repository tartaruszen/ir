## IR -- Indescribable Redirector.

#### What is this?

+ Just like [ss-redir](https://github.com/shadowsocks/shadowsocks-libev#advanced-usage)
+ This is an indescribable program can be used to do something indescribable.

#### Who need this?

+ Chinese Steam users. (Yeah, such as me.

#### Supported platform

+ Linux only

#### Requirements

+ Linux Kernel 2.6.17+
+ Python 3.6.0+
+ libcrypto.so.1.1 (OpenSSL 1.1)

##### Copyright & License

+ For most of code/files: Void Copyright and Void License. You can do whatever you want to do.
+ Special: The following functions are copied from [shadowsocks](https://github.com/shadowsocks/shadowsocks/blob/master/shadowsocks/tcprelay.py#L110) and licensed under [Apache License v2.0](https://www.apache.org/licenses/LICENSE-2.0). View the code for details.
  * [ir.handler.TCPHandler.\_write\_to\_sock](https://github.com/Mr-indescribable/ir/blob/master/ir/handler.py#L121)
  * [ir.handler.TCPHandler.\_on\_local\_read](https://github.com/Mr-indescribable/ir/blob/master/ir/handler.py#L154)
  * [ir.handler.TCPHandler.\_on\_remote\_write](https://github.com/Mr-indescribable/ir/blob/master/ir/handler.py#L196)
  * [ir.handler.TCPHandler.\_on\_remote\_read](https://github.com/Mr-indescribable/ir/blob/master/ir/handler.py#L215)
  * [ir.handler.TCPHandler.\_on\_local\_write](https://github.com/Mr-indescribable/ir/blob/master/ir/handler.py#L251)

----------------------------

> I don't need security, I need network quality.
