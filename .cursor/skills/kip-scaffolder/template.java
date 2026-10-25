/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements. See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License. You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.kafka.example.kip;

import org.apache.kafka.common.config.AbstractConfig;
import org.apache.kafka.common.config.ConfigDef;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Map;

import static org.apache.kafka.common.config.ConfigDef.Importance.MEDIUM;
import static org.apache.kafka.common.config.ConfigDef.Type.INT;

/**
 * Copy this file into the module the KIP names, then rename the class, package, and config key.
 *
 * <p>This copy lives under {@code .cursor/skills/} and is not part of a Gradle source set, so
 * {@code checkstyleMain} and {@code compileJava} do not see it until it is moved under
 * {@code src/main/java}.
 */
public class KipConfigSkeleton extends AbstractConfig {

    private static final Logger log = LoggerFactory.getLogger(KipConfigSkeleton.class);

    public static final String EXAMPLE_TIMEOUT_MS_CONFIG = "example.timeout.ms";
    public static final int EXAMPLE_TIMEOUT_MS_DEFAULT = 30000;
    public static final String EXAMPLE_TIMEOUT_MS_DOC =
            "Maximum time in milliseconds the component waits for the KIP-defined operation.";

    public static final ConfigDef CONFIG = new ConfigDef()
            .define(EXAMPLE_TIMEOUT_MS_CONFIG, INT, EXAMPLE_TIMEOUT_MS_DEFAULT, MEDIUM, EXAMPLE_TIMEOUT_MS_DOC);

    public KipConfigSkeleton(final Map<?, ?> originals) {
        super(CONFIG, originals);
    }

    public int timeoutMs() {
        final int timeoutMs = getInt(EXAMPLE_TIMEOUT_MS_CONFIG);
        log.debug("Resolved {}={}", EXAMPLE_TIMEOUT_MS_CONFIG, timeoutMs);
        return timeoutMs;
    }
}
