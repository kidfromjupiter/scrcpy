package com.genymobile.scrcpy.video;

import com.genymobile.scrcpy.model.ConfigurationException;

import org.junit.Assert;
import org.junit.Test;

public class CameraCaptureTest {

    @Test
    public void testResolveExposureCompensation() throws ConfigurationException {
        Assert.assertEquals(-4, CameraCapture.resolveExposureCompensation(-1.4f, -6, 6, 1f / 3));
        Assert.assertEquals(2, CameraCapture.resolveExposureCompensation(0.6f, -6, 6, 1f / 3));
    }

    @Test(expected = ConfigurationException.class)
    public void testExposureCompensationOutOfRange() throws ConfigurationException {
        CameraCapture.resolveExposureCompensation(3, -6, 6, 1f / 3);
    }
}
