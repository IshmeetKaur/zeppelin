#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import sys, getopt, traceback, json, re

from py4j.java_gateway import java_import, JavaGateway, GatewayClient
from py4j.protocol import Py4JJavaError
from pyspark.conf import SparkConf
from pyspark.context import SparkContext
from pyspark.rdd import RDD
from pyspark.files import SparkFiles
from pyspark.storagelevel import StorageLevel
from pyspark.accumulators import Accumulator, AccumulatorParam
from pyspark.broadcast import Broadcast
from pyspark.serializers import MarshalSerializer, PickleSerializer
import ast
import traceback
import base64
from io import BytesIO
try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

# for back compatibility
from pyspark.sql import SQLContext, HiveContext, Row

class Logger(object):
  def __init__(self):
    pass

  def write(self, message):
    intp.appendOutput(message)

  def reset(self):
    pass

  def flush(self):
    pass


class PyZeppelinContext(dict):
  def __init__(self, zc):
    self.z = zc
    self.max_result = 1000

  def show(self, obj,**kwargs):
    from pyspark.sql import DataFrame
    if isinstance(obj, DataFrame) and type(obj).__name__ == "DataFrame":
      print(gateway.jvm.org.apache.zeppelin.spark.ZeppelinContext.showDF(self.z, obj._jdf))    
    elif hasattr(obj, '__name__') and obj.__name__ == "matplotlib.pyplot":
      self.show_matplotlib(obj, **kwargs)    
    elif hasattr(obj, '__call__'):
      obj() #error reporting
    else:
      print(str(obj))

  # By implementing special methods it makes operating on it more Pythonic
  def __setitem__(self, key, item):
    self.z.put(key, item)

  def __getitem__(self, key):
    return self.z.get(key)

  def __delitem__(self, key):
    self.z.remove(key)

  def __contains__(self, item):
    return self.z.containsKey(item)  
    
  def show_matplotlib(self, p, fmt="png", width="auto", height="auto", 
                      **kwargs):
    """Matplotlib show function
    """
    if fmt == "png":
      img = BytesIO()
      p.savefig(img, format=fmt)
      img_str = b"data:image/png;base64,"
      img_str += base64.b64encode(img.getvalue().strip())
      img_tag = "<img src={img} style='width={width};height:{height}'>"
      # Decoding is necessary for Python 3 compability
      img_str = img_str.decode("ascii")
      img_str = img_tag.format(img=img_str, width=width, height=height)
    elif fmt == "svg":
      img = StringIO()
      p.savefig(img, format=fmt)
      img_str = img.getvalue()
    else:
      raise ValueError("fmt must be 'png' or 'svg'")
    
    html = "%html <div style='width:{width};height:{height}'>{img}<div>"
    print(html.format(width=width, height=height, img=img_str))
    img.close()

  def add(self, key, value):
    self.__setitem__(key, value)

  def put(self, key, value):
    self.__setitem__(key, value)

  def get(self, key):
    return self.__getitem__(key)

  def input(self, name, defaultValue = ""):
    return self.z.input(name, defaultValue)

  def select(self, name, options, defaultValue = ""):
    # auto_convert to ArrayList doesn't match the method signature on JVM side
    tuples = list(map(lambda items: self.__tupleToScalaTuple2(items), options))
    iterables = gateway.jvm.scala.collection.JavaConversions.collectionAsScalaIterable(tuples)
    return self.z.select(name, defaultValue, iterables)

  def checkbox(self, name, options, defaultChecked = None):
    if defaultChecked is None:
      defaultChecked = list(map(lambda items: items[0], options))
    optionTuples = list(map(lambda items: self.__tupleToScalaTuple2(items), options))
    optionIterables = gateway.jvm.scala.collection.JavaConversions.collectionAsScalaIterable(optionTuples)
    defaultCheckedIterables = gateway.jvm.scala.collection.JavaConversions.collectionAsScalaIterable(defaultChecked)

    checkedIterables = self.z.checkbox(name, defaultCheckedIterables, optionIterables)
    return gateway.jvm.scala.collection.JavaConversions.asJavaCollection(checkedIterables)

  def __tupleToScalaTuple2(self, tuple):
    if (len(tuple) == 2):
      return gateway.jvm.scala.Tuple2(tuple[0], tuple[1])
    else:
      raise IndexError("options must be a list of tuple of 2")


class SparkVersion(object):
  SPARK_1_4_0 = 10400
  SPARK_1_3_0 = 10300
  SPARK_2_0_0 = 20000

  def __init__(self, versionNumber):
    self.version = versionNumber

  def isAutoConvertEnabled(self):
    return self.version >= self.SPARK_1_4_0

  def isImportAllPackageUnderSparkSql(self):
    return self.version >= self.SPARK_1_3_0

  def isSpark2(self):
    return self.version >= self.SPARK_2_0_0

class PySparkCompletion:
  def __init__(self, interpreterObject):
    self.interpreterObject = interpreterObject

  def getGlobalCompletion(self):
    objectDefList = []
    try:
      for completionItem in list(globals().keys()):
        objectDefList.append(completionItem)
    except:
      return None
    else:
      return objectDefList

  def getMethodCompletion(self, text_value):
    execResult = locals()
    if text_value == None:
      return None
    completion_target = text_value
    try:
      if len(completion_target) <= 0:
        return None
      if text_value[-1] == ".":
        completion_target = text_value[:-1]
      exec("{} = dir({})".format("objectDefList", completion_target), globals(), execResult)
    except:
      return None
    else:
      return list(execResult['objectDefList'])


  def getCompletion(self, text_value):
    completionList = set()

    globalCompletionList = self.getGlobalCompletion()
    if globalCompletionList != None:
      for completionItem in list(globalCompletionList):
        completionList.add(completionItem)

    if text_value != None:
      objectCompletionList = self.getMethodCompletion(text_value)
      if objectCompletionList != None:
        for completionItem in list(objectCompletionList):
          completionList.add(completionItem)
    if len(completionList) <= 0:
      self.interpreterObject.setStatementsFinished("", False)
    else:
      result = json.dumps(list(filter(lambda x : not re.match("^__.*", x), list(completionList))))
      self.interpreterObject.setStatementsFinished(result, False)


output = Logger()
sys.stdout = output
sys.stderr = output

client = GatewayClient(port=int(sys.argv[1]))
sparkVersion = SparkVersion(int(sys.argv[2]))

if sparkVersion.isSpark2():
  from pyspark.sql import SparkSession
else:
  from pyspark.sql import SchemaRDD


if sparkVersion.isAutoConvertEnabled():
  gateway = JavaGateway(client, auto_convert = True)
else:
  gateway = JavaGateway(client)

java_import(gateway.jvm, "org.apache.spark.SparkEnv")
java_import(gateway.jvm, "org.apache.spark.SparkConf")
java_import(gateway.jvm, "org.apache.spark.api.java.*")
java_import(gateway.jvm, "org.apache.spark.api.python.*")
java_import(gateway.jvm, "org.apache.spark.mllib.api.python.*")

intp = gateway.entry_point
intp.onPythonScriptInitialized()

jsc = intp.getJavaSparkContext()

if sparkVersion.isImportAllPackageUnderSparkSql():
  java_import(gateway.jvm, "org.apache.spark.sql.*")
  java_import(gateway.jvm, "org.apache.spark.sql.hive.*")
else:
  java_import(gateway.jvm, "org.apache.spark.sql.SQLContext")
  java_import(gateway.jvm, "org.apache.spark.sql.hive.HiveContext")
  java_import(gateway.jvm, "org.apache.spark.sql.hive.LocalHiveContext")
  java_import(gateway.jvm, "org.apache.spark.sql.hive.TestHiveContext")


java_import(gateway.jvm, "scala.Tuple2")

jconf = intp.getSparkConf()
conf = SparkConf(_jvm = gateway.jvm, _jconf = jconf)
sc = SparkContext(jsc=jsc, gateway=gateway, conf=conf)
sqlc = SQLContext(sc, intp.getSQLContext())
sqlContext = sqlc

if sparkVersion.isSpark2():
  spark = SparkSession(sc, intp.getSparkSession())

completion = PySparkCompletion(intp)
z = PyZeppelinContext(intp.getZeppelinContext())

while True :
  req = intp.getStatements()
  try:
    stmts = req.statements().split("\n")
    jobGroup = req.jobGroup()
    final_code = []

    for s in stmts:
      if s == None:
        continue

      # skip comment
      s_stripped = s.strip()
      if len(s_stripped) == 0 or s_stripped.startswith("#"):
        continue

      final_code.append(s)

    if final_code:
      # use exec mode to compile the statements except the last statement,
      # so that the last statement's evaluation will be printed to stdout
      sc.setJobGroup(jobGroup, "Zeppelin")
      code = compile('\n'.join(final_code), '<stdin>', 'exec', ast.PyCF_ONLY_AST, 1)
      to_run_exec, to_run_single = code.body[:-1], code.body[-1:]

      try:
        for node in to_run_exec:
          mod = ast.Module([node])
          code = compile(mod, '<stdin>', 'exec')
          exec(code)

        for node in to_run_single:
          mod = ast.Interactive([node])
          code = compile(mod, '<stdin>', 'single')
          exec(code)
      except:
        raise Exception(traceback.format_exc())

    intp.setStatementsFinished("", False)
  except Py4JJavaError:
    excInnerError = traceback.format_exc() # format_tb() does not return the inner exception
    innerErrorStart = excInnerError.find("Py4JJavaError:")
    if innerErrorStart > -1:
       excInnerError = excInnerError[innerErrorStart:]
    intp.setStatementsFinished(excInnerError + str(sys.exc_info()), True)
  except:
    intp.setStatementsFinished(traceback.format_exc(), True)

  output.reset()
