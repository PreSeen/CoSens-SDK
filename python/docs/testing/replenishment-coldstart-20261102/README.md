# 无Memory冷启动首轮

实际使用已安装的cosens-sdk 0.3.0、US线上服务、deepseek-v4-flash。使用新的企业命名空间，没有写入Memory、没有Memory检索工具、没有Recipe、没有反馈。只调用一次模型，保留原始请求及响应。

首轮耗尽8192输出token，finish_reason为length，全部输出token用于推理，未交付最终答案。此结果属于交付失败，不能证明具体业务推理错误。没有为获得错误答案而重试模型或修改配置。

SDK创建Task时发生两次TLS连接超时，使用同一幂等键恢复；这些不是模型执行，也未作为业务失败计分。模型执行失败状态通过SDK提交，回执见evaluation.json。

文件：model-request.json为实际模型输入；model-response.json为原始响应；state.json保存SDK回执；evaluation.json为核验说明。
